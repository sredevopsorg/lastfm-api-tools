"""Jellyfin REST client.

Keeps three concerns out of the rest of the codebase: transport retries, the
authentication header, and the translation of upstream failures into our own
error hierarchy.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Mapping, Sequence
from typing import Any, Self

import httpx
import structlog

from metaedit.adapters.jellyfin.dto import (
    BaseItemDto,
    BaseItemDtoQueryResult,
    ItemKind,
    MetadataEditorInfo,
    SystemInfoPublic,
    UserDto,
    VirtualFolderInfo,
)
from metaedit.config import Settings
from metaedit.domain.errors import (
    NotFoundError,
    UpstreamAuthError,
    UpstreamContractError,
    UpstreamTimeout,
    UpstreamUnavailable,
)

log = structlog.get_logger(__name__)

# Live-verified against Jellyfin 12.2.0: authentication uses the custom
# `MediaBrowser` scheme on the `Authorization` header, with comma-separated
# quoted parameters. A bare `Authorization: <key>` is rejected (401), and the
# legacy channels (`X-Emby-Token`, `X-MediaBrowser-Token`, `?api_key=`) are
# disabled from Jellyfin 12 onward by the `EnableLegacyAuthorization` kill-switch.
#
# The header carries client identity as well as the credential. `Token` is appended
# only once a key exists, which is what lets the same builder serve an unauthenticated
# probe.
AUTH_SCHEME = "MediaBrowser"

# Identity reported to the server, so sessions and logs name this tool rather than
# appearing anonymous. `DeviceId` must be stable: Jellyfin permits one access token
# per device id and re-authenticating the same pair revokes the previous token.
CLIENT_NAME = "metaedit"
DEVICE_NAME = "metaedit"
DEVICE_ID = "metaedit-desktop"
CLIENT_VERSION = "0.1.0"

# Fields requested explicitly so one batched call hydrates the full write payload.
#
# Every whitelist field is requested deliberately: an omitted field would come
# back absent, and a snapshot that does not know a field's current value cannot
# safely round-trip it (ADR 0003).
ITEM_FIELDS: tuple[str, ...] = (
    # Etag is what optimistic concurrency needs (ADR 0007). Live-verified on 12.2.0:
    # items DO carry an Etag, but only when it is requested -- without it here the
    # client saw None and concluded there was no concurrency token at all.
    # DateLastSaved and DateLastRefreshed are NOT returned for music items even when
    # requested, so Etag is the only usable version token.
    "Etag",
    "Genres",
    "Tags",
    "ProviderIds",
    "ExternalUrls",
    "Overview",
    "OriginalTitle",
    "Studios",
    "ProductionLocations",
    "People",
    "DateCreated",
    "DateLastSaved",
    "DateLastRefreshed",
    "ChildCount",
    "RecursiveItemCount",
    "ParentId",
    "Path",
)

_RETRY_STATUSES = frozenset({502, 503, 504})
_MAX_BACKOFF_S = 4.0


class JellyfinClient:
    """Async Jellyfin API client. Use as an async context manager."""

    def __init__(self, settings: Settings, *, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._base = settings.jellyfin_base_url
        self._key = settings.jellyfin_key()
        self._timeout = settings.jellyfin_timeout_s
        self._max_retries = max(settings.jellyfin_max_retries, 0)
        self._client = client
        self._owns_client = client is None

    async def __aenter__(self) -> Self:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout, follow_redirects=False)
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------ core

    def _headers(self, *, include_token: bool = True) -> dict[str, str]:
        """Request headers, including the MediaBrowser authorization scheme.

        ``include_token=False`` produces the identity-only header that Jellyfin
        requires *before* a credential exists; sending a bare token without the scheme
        is parsed as a malformed scheme and rejected.
        """
        return {
            "Accept": "application/json",
            "User-Agent": f"{CLIENT_NAME}/{CLIENT_VERSION}",
            "Authorization": build_authorization_header(self._key if include_token else None),
        }

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Any | None = None,
        auth_required: bool = True,
        sentinel_404: bool = False,
        raw: bool = False,
    ) -> httpx.Response:
        if self._client is None:
            msg = "JellyfinClient must be used as an async context manager"
            raise RuntimeError(msg)
        if auth_required and not self._key:
            raise UpstreamAuthError("No Jellyfin API key is configured.")

        url = f"{self._base}{path}"
        attempt = 0
        while True:
            try:
                response = await self._client.request(
                    method,
                    url,
                    params=_clean_params(params),
                    json=json_body,
                    headers=self._headers(),
                )
            except httpx.TimeoutException as exc:
                if attempt < self._max_retries:
                    attempt += 1
                    await self._sleep_backoff(attempt)
                    continue
                raise UpstreamTimeout(f"Jellyfin timed out at {path}") from exc
            except httpx.HTTPError as exc:
                if attempt < self._max_retries:
                    attempt += 1
                    await self._sleep_backoff(attempt)
                    continue
                raise UpstreamUnavailable(
                    f"Could not reach Jellyfin at {path}", detail=type(exc).__name__
                ) from exc

            if response.status_code in (401, 403) and auth_required:
                raise UpstreamAuthError(
                    "Jellyfin rejected the API key. Metadata writes require an administrator "
                    "key: POST /Items/{itemId} is gated on the RequiresElevation policy.",
                    upstream_status=response.status_code,
                )

            if response.status_code in _RETRY_STATUSES and attempt < self._max_retries:
                attempt += 1
                await self._sleep_backoff(attempt, response.headers.get("Retry-After"))
                continue

            if raw:
                # The caller wants to interpret the status itself: a 400 is a
                # meaningful answer here, not an error to raise.
                return response

            if response.status_code == 404 and sentinel_404:
                return response
            if response.status_code == 404:
                raise NotFoundError(f"Jellyfin has no resource at {path}")

            if response.status_code >= 500:
                raise UpstreamUnavailable(
                    f"Jellyfin returned {response.status_code} for {path}",
                    upstream_status=response.status_code,
                )
            if response.status_code >= 400:
                raise UpstreamContractError(
                    f"Jellyfin rejected the request to {path} with {response.status_code}",
                    upstream_status=response.status_code,
                    detail=_sanitise_body(response),
                )
            return response

    def _sleep_seconds(self, attempt: int, retry_after: str | None) -> float:
        if retry_after:
            try:
                return min(float(retry_after), _MAX_BACKOFF_S)
            except ValueError:
                pass
        base: float = min(0.25 * (2 ** (attempt - 1)), _MAX_BACKOFF_S)
        return base * (0.5 + random.random() / 2)  # jitter

    async def _sleep_backoff(self, attempt: int, retry_after: str | None = None) -> None:
        delay = self._sleep_seconds(attempt, retry_after)
        log.warning("jellyfin_retry", attempt=attempt, delay_s=round(delay, 3))
        await asyncio.sleep(delay)

    async def _get_json(
        self,
        path: str,
        params: Mapping[str, Any] | None = None,
        *,
        auth_required: bool = True,
    ) -> Any:
        response = await self._request("GET", path, params=params, auth_required=auth_required)
        try:
            return response.json()
        except ValueError as exc:
            raise UpstreamContractError(
                f"Jellyfin returned non-JSON for {path}", detail=_sanitise_body(response)
            ) from exc

    # ----------------------------------------------------------------- probes

    async def system_info_public(self) -> SystemInfoPublic:
        payload = await self._get_json("/System/Info/Public", auth_required=False)
        return SystemInfoPublic.model_validate(payload)

    async def current_user(self) -> UserDto:
        """The authenticated user.

        Requires a **user access token**. An API key is userless, so this endpoint
        answers 400 ``Token is not owned by a user.`` (live-verified on 12.2.0). Use
        :meth:`can_write_metadata` to establish capability regardless of credential
        type.
        """
        payload = await self._get_json("/Users/Me")
        return UserDto.model_validate(payload)

    async def can_write_metadata(self) -> tuple[bool, str | None]:
        """Whether the configured credential can actually perform an item update.

        Two credential families reach this method, and they answer differently:

        * An **API key** is userless, so ``GET /Users/Me`` returns **400** with
          ``Token is not owned by a user.`` (live-verified on 12.2.0). That is a
          *success* signal, not a failure: an API key bypasses user identity and
          carries administrator privileges, which is exactly what
          ``POST /Items/{itemId}`` requires.
        * A **user access token** returns 200 and reports ``IsAdministrator``.

        Treating the API-key 400 as an error made every API-key deployment look
        broken, which is why this inspects the response rather than catching an
        exception.
        """
        if not self._key:
            return False, "no_api_key_configured"
        try:
            response = await self._request("GET", "/Users/Me", raw=True)
        except UpstreamAuthError:
            return False, "key_rejected"
        except (UpstreamUnavailable, UpstreamTimeout):
            return False, "unreachable"

        if response.status_code == 200:
            try:
                policy = response.json().get("Policy") or {}
            except ValueError:
                return False, "unexpected_response"
            if policy.get("IsAdministrator"):
                return True, None
            return False, "user_is_not_an_administrator"

        if response.status_code == 400:
            # A 400 here means the credential is userless, which is what an API key
            # is. The body is a generic RFC9110 ProblemDetails and does NOT name the
            # reason (live-verified on 12.2.0), so the credential is confirmed by a
            # second probe rather than by parsing text that is not there.
            probe = await self._request("GET", "/System/Info", raw=True)
            if probe.status_code == 200:
                # API keys bypass user identity and carry administrator privileges,
                # which is exactly what POST /Items/{itemId} requires.
                return True, None
            return False, "credential_not_accepted_for_authenticated_reads"

        if response.status_code in (401, 403):
            return False, "key_rejected"
        return False, f"unexpected_status_{response.status_code}"

    # ------------------------------------------------------------------ media

    async def music_libraries(self) -> list[VirtualFolderInfo]:
        """Music libraries, with the ids needed to browse inside them.

        Uses ``/Library/VirtualFolders`` rather than ``/Library/MediaFolders``:
        live-verified on 12.2.0, MediaFolders returned ``ItemId: null`` for every
        library, so there was no id to scope a browse by, while VirtualFolders
        returned it. Models are tolerant of either shape.
        """
        payload = await self._get_json("/Library/VirtualFolders")
        entries = payload if isinstance(payload, list) else payload.get("Items", [])
        folders = [VirtualFolderInfo.model_validate(item) for item in entries]
        return [folder for folder in folders if folder.CollectionType == "music"]

    async def artists(
        self,
        *,
        parent_id: str | None = None,
        search_term: str | None = None,
        start_index: int = 0,
        limit: int = 100,
        sort_by: Sequence[str] = ("SortName",),
        user_id: str | None = None,
    ) -> BaseItemDtoQueryResult:
        """Browse album artists.

        **No ``includeItemTypes``.** ``/Artists`` already returns artists, and
        live-verified on 12.2.0 passing ``includeItemTypes=MusicArtist`` makes it
        return zero results while a bare call returns all 594. The filter that looks
        harmless is the one that silently empties the browse.
        """
        params: dict[str, Any] = {
            "parentId": parent_id,
            "searchTerm": search_term,
            "startIndex": start_index,
            "limit": limit,
            "sortBy": ",".join(sort_by),
            "sortOrder": "Ascending",
            "recursive": "true",
            "fields": ",".join(ITEM_FIELDS),
            "enableTotalRecordCount": "true",
            "userId": user_id,
        }
        payload = await self._get_json("/Artists", params)
        return BaseItemDtoQueryResult.model_validate(payload)

    async def items(
        self,
        *,
        kind: ItemKind,
        parent_id: str | None = None,
        search_term: str | None = None,
        start_index: int = 0,
        limit: int = 100,
        sort_by: Sequence[str] = ("SortName",),
        user_id: str | None = None,
    ) -> BaseItemDtoQueryResult:
        params: dict[str, Any] = {
            "parentId": parent_id,
            "searchTerm": search_term,
            "startIndex": start_index,
            "limit": limit,
            "sortBy": ",".join(sort_by),
            "sortOrder": "Ascending",
            "recursive": "true",
            "includeItemTypes": kind,
            "fields": ",".join(ITEM_FIELDS),
            "enableTotalRecordCount": "true",
            "userId": user_id,
        }
        payload = await self._get_json("/Items", params)
        return BaseItemDtoQueryResult.model_validate(payload)

    async def items_by_ids(
        self, item_ids: Sequence[str], *, user_id: str | None = None
    ) -> list[BaseItemDto]:
        """Hydrate many items in one call -- the batched multi-select read."""
        if not item_ids:
            return []
        results: list[BaseItemDto] = []
        # Keep URLs comfortably short; 200 ids per request is generous.
        for chunk in _chunks(item_ids, 200):
            payload = await self._get_json(
                "/Items",
                {
                    "ids": ",".join(chunk),
                    "recursive": "true",
                    "fields": ",".join(ITEM_FIELDS),
                    "userId": user_id,
                },
            )
            results.extend(
                BaseItemDto.model_validate(item)
                for item in BaseItemDtoQueryResult.model_validate(payload).Items
            )
        return results

    async def item(self, item_id: str, *, user_id: str | None = None) -> BaseItemDto:
        payload = await self._get_json(
            f"/Items/{item_id}", {"userId": user_id, "fields": ",".join(ITEM_FIELDS)}
        )
        return BaseItemDto.model_validate(payload)

    async def metadata_editor_info(self, item_id: str) -> MetadataEditorInfo:
        payload = await self._get_json(f"/Items/{item_id}/MetadataEditor")
        return MetadataEditorInfo.model_validate(payload)

    # ------------------------------------------------------------------ write

    async def update_item(self, item_id: str, payload: dict[str, Any]) -> None:
        """Full-overwrite item update. The caller owns payload completeness."""
        await self._request("POST", f"/Items/{item_id}", json_body=payload)

    async def refresh_item(
        self,
        item_id: str,
        *,
        metadata_refresh_mode: str = "ValidationOnly",
        image_refresh_mode: str = "None",
        replace_all_metadata: bool = False,
        replace_all_images: bool = False,
    ) -> None:
        await self._request(
            "POST",
            f"/Items/{item_id}/Refresh",
            params={
                "metadataRefreshMode": metadata_refresh_mode,
                "imageRefreshMode": image_refresh_mode,
                "replaceAllMetadata": str(replace_all_metadata).lower(),
                "replaceAllImages": str(replace_all_images).lower(),
            },
        )


def build_authorization_header(token: str | None) -> str:
    """The `MediaBrowser` authorization header Jellyfin expects.

    Values are quoted and percent-encoded, matching the official SDKs: the server
    URL-decodes them after parsing, and an unencoded quote or comma in a value would
    otherwise break the comma-separated parameter list.
    """
    from urllib.parse import quote

    def param(key: str, value: str) -> str:
        return f'{key}="{quote(value, safe="")}"'

    parts = [
        param("Client", CLIENT_NAME),
        param("Device", DEVICE_NAME),
        param("DeviceId", DEVICE_ID),
        param("Version", CLIENT_VERSION),
    ]
    if token:
        parts.append(param("Token", token))
    return f"{AUTH_SCHEME} " + ", ".join(parts)


def _clean_params(params: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if params is None:
        return None
    return {key: value for key, value in params.items() if value is not None}


def _chunks(items: Sequence[str], size: int) -> list[list[str]]:
    return [list(items[index : index + size]) for index in range(0, len(items), size)]


def _sanitise_body(response: httpx.Response, *, limit: int = 500) -> str:
    """Server-side diagnostic text. Never sent to a client verbatim."""
    try:
        text = response.text
    except Exception:
        return "<unreadable>"
    return text[:limit]
