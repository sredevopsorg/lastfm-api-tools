"""Last.fm Web Services client.

Read-only by design (ADR 0012): an API key, no user auth, no request signing.

Every call goes through ``_call``, which enforces one rule: the response is
archived **before** it is interpreted. If parsing then fails, the payload is
already durable and can be re-derived later without spending another request.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Self

import httpx
import structlog

from metaedit.adapters.lastfm.canonical import is_error_body
from metaedit.adapters.lastfm.models import (
    FATAL_ERROR_CODES,
    RETRYABLE_ERROR_CODES,
    LastfmAlbum,
    LastfmArtist,
    LastfmSearchResult,
    LastfmSimilarArtist,
    LastfmTopTags,
    LastfmTrack,
    parse_album,
    parse_album_search,
    parse_artist,
    parse_artist_search,
    parse_error,
    parse_similar,
    parse_top_tags,
    parse_track,
)
from metaedit.adapters.lastfm.ratelimit import TokenBucket
from metaedit.archive.store import ArchiveStore, Observation
from metaedit.config import Settings
from metaedit.domain.errors import (
    LastfmAuthError,
    LastfmContractError,
    LastfmNotFound,
    LastfmThrottled,
)

log = structlog.get_logger(__name__)

# One bucket for the process: parallel callers must not collectively exceed the
# documented per-IP guidance.
_shared_buckets: dict[tuple[float, int], TokenBucket] = {}


def shared_bucket(rate: float, burst: int) -> TokenBucket:
    key = (rate, burst)
    if key not in _shared_buckets:
        _shared_buckets[key] = TokenBucket(rate, burst)
    return _shared_buckets[key]


def reset_shared_buckets() -> None:
    """Test hook: drop the process-wide buckets."""
    _shared_buckets.clear()


@dataclass(slots=True)
class LastfmResult:
    """A payload plus where it came from.

    ``served_from_archive`` is surfaced to the UI so a user can tell why a result
    appeared instantly and whether it might be stale.
    """

    body: dict[str, Any]
    served_from_archive: bool
    response_id: str | None = None
    request_id: int | None = None
    first_seen_at: str | None = None
    last_seen_at: str | None = None
    observation_count: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def provenance(self) -> dict[str, Any]:
        return {
            "served_from_archive": self.served_from_archive,
            "response_id": self.response_id,
            "request_id": self.request_id,
            "first_seen_at": self.first_seen_at,
            "last_seen_at": self.last_seen_at,
            "observation_count": self.observation_count,
        }


class LastfmClient:
    """Async Last.fm client. Use as an async context manager."""

    def __init__(
        self,
        settings: Settings,
        *,
        store: ArchiveStore | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._key = settings.lastfm_key()
        self._base = settings.lastfm_base_url
        self._timeout = settings.lastfm_timeout_s
        self._max_retries = max(settings.lastfm_max_retries, 0)
        self._client = client
        self._owns_client = client is None
        self._bucket = shared_bucket(settings.lastfm_max_rps, settings.lastfm_burst)

    async def __aenter__(self) -> Self:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self._timeout,
                headers={
                    "User-Agent": self._settings.lastfm_user_agent,
                    "Accept": "application/json",
                },
            )
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------ core

    async def _call(
        self,
        method: str,
        params: dict[str, Any],
        *,
        max_age: timedelta | None = None,
        allow_archive: bool = True,
    ) -> LastfmResult:
        """Archive-first read-through, then a rate-limited, retried fetch.

        The identity used for archive lookup is the *complete* parameter set --
        including the defaults the client always sends, such as ``autocorrect``.
        A partial identity would hash a request differently from the way the same
        request was stored, and the archive would look empty forever.
        """
        params = {**params, **_always_sent_params()}
        if allow_archive and self._store is not None and max_age is not None:
            cached = await self._store.find_recent(method, params, max_age=max_age)
            if cached is not None and not is_error_body(cached.body):
                # Record the read so the timeline shows it was served offline.
                await self._store.record(
                    Observation(
                        method=method,
                        params=params,
                        http_status=None,
                        duration_ms=0,
                        body=None,
                        served_from_archive=True,
                        user_agent=self._settings.lastfm_user_agent,
                    )
                )
                return self._result_from_archive(cached)

        return await self._fetch(method, params, max_age=max_age)

    async def _fetch(
        self, method: str, params: dict[str, Any], *, max_age: timedelta | None
    ) -> LastfmResult:
        if self._client is None:
            msg = "LastfmClient must be used as an async context manager"
            raise RuntimeError(msg)
        if not self._key:
            raise LastfmAuthError(
                "No Last.fm API key is configured. Create one at "
                "https://www.last.fm/api/account/create and set LASTFM_API_KEY."
            )

        query = {"method": method, "api_key": self._key, "format": "json"}
        query.update({key: value for key, value in params.items() if value not in (None, "")})

        attempt = 0
        while True:
            await self._bucket.acquire()
            started = asyncio.get_running_loop().time()
            try:
                response = await self._client.get(self._base, params=query)
            except httpx.TimeoutException as exc:
                duration_ms = int((asyncio.get_running_loop().time() - started) * 1000)
                await self._record(method, params, None, duration_ms, None, None, attempt)
                if attempt < self._max_retries:
                    attempt += 1
                    await self._backoff(attempt)
                    continue
                raise LastfmThrottled(
                    "Last.fm timed out", lastfm_code=None, detail="timeout"
                ) from exc
            except httpx.HTTPError as exc:
                duration_ms = int((asyncio.get_running_loop().time() - started) * 1000)
                await self._record(method, params, None, duration_ms, None, None, attempt)
                if attempt < self._max_retries:
                    attempt += 1
                    await self._backoff(attempt)
                    continue
                raise LastfmThrottled("Could not reach Last.fm", detail=type(exc).__name__) from exc

            duration_ms = int((asyncio.get_running_loop().time() - started) * 1000)
            body = self._decode(response, method)

            if body is None:
                # Not JSON: a transport-level failure, retried like a 5xx.
                await self._record(
                    method, params, response.status_code, duration_ms, None, None, attempt
                )
                if attempt < self._max_retries:
                    attempt += 1
                    await self._backoff(attempt, response.headers.get("Retry-After"))
                    continue
                raise LastfmThrottled(
                    f"Last.fm returned HTTP {response.status_code} or a non-JSON body",
                    detail=f"http_{response.status_code}",
                ) from None

            if not is_error_body(body):
                # Archive before interpreting: the payload is durable either way.
                archived = await self._record_and_archive(
                    method, params, response.status_code, duration_ms, body, None, attempt
                )
                return self._result_from_body(body, archived)

            code, message = parse_error(body)
            await self._record(
                method, params, response.status_code, duration_ms, body, code, attempt
            )

            if code in FATAL_ERROR_CODES:
                raise LastfmAuthError(f"Last.fm rejected the API key: {message}", lastfm_code=code)
            if code in (6, 7):
                # A normal, archived outcome: not found, or bad params.
                raise LastfmNotFound(f"Last.fm has no such resource ({message})", lastfm_code=code)
            if code in RETRYABLE_ERROR_CODES and attempt < self._max_retries:
                attempt += 1
                await self._backoff(attempt, response.headers.get("Retry-After"))
                continue
            raise LastfmThrottled(f"Last.fm error {code}: {message}", lastfm_code=code)

    def _decode(self, response: httpx.Response, method: str) -> dict[str, Any] | None:
        try:
            payload = response.json()
        except ValueError:
            log.warning(
                "lastfm_non_json",
                method=method,
                status=response.status_code,
                body=response.text[:200],
            )
            return None
        if not isinstance(payload, dict):
            return None
        return payload

    async def _record(
        self,
        method: str,
        params: dict[str, Any],
        http_status: int | None,
        duration_ms: int | None,
        body: dict[str, Any] | None,
        error_code: int | None,
        attempt: int,
    ) -> None:
        """Persist an attempt that produced no body (or only an error body)."""
        if self._store is None:
            return
        await self._store.record(
            Observation(
                method=method,
                params=params,
                http_status=http_status,
                duration_ms=duration_ms,
                body=body,
                lastfm_error_code=error_code,
                retry_count=attempt,
                user_agent=self._settings.lastfm_user_agent,
            )
        )

    async def _record_and_archive(
        self,
        method: str,
        params: dict[str, Any],
        http_status: int | None,
        duration_ms: int | None,
        body: dict[str, Any],
        error_code: int | None,
        attempt: int,
    ) -> Any:
        if self._store is None:
            return None
        return await self._store.record(
            Observation(
                method=method,
                params=params,
                http_status=http_status,
                duration_ms=duration_ms,
                body=body,
                lastfm_error_code=error_code,
                retry_count=attempt,
                user_agent=self._settings.lastfm_user_agent,
            )
        )

    def _result_from_archive(self, archived: Any) -> LastfmResult:
        log.info("lastfm_archive_hit", response_id=archived.response_id)
        return LastfmResult(
            body=archived.body,
            served_from_archive=True,
            response_id=archived.response_id,
            request_id=archived.request_id,
            first_seen_at=archived.first_seen_at.isoformat(),
            last_seen_at=archived.last_seen_at.isoformat(),
            observation_count=archived.observation_count,
        )

    def _result_from_body(self, body: dict[str, Any], archived: Any) -> LastfmResult:
        if archived is None:
            return LastfmResult(body=body, served_from_archive=False)
        return LastfmResult(
            body=body,
            served_from_archive=False,
            response_id=archived.response_id,
            request_id=archived.request_id,
            first_seen_at=archived.first_seen_at.isoformat(),
            last_seen_at=archived.last_seen_at.isoformat(),
            observation_count=archived.observation_count,
        )

    async def _backoff(self, attempt: int, retry_after: str | None = None) -> None:
        delay = _backoff_seconds(attempt, retry_after)
        log.warning("lastfm_retry", attempt=attempt, delay_s=round(delay, 3))
        await asyncio.sleep(delay)

    # --------------------------------------------------------------- methods

    def _freshness(self, method: str) -> timedelta:
        if method == "artist.getsimilar":
            return timedelta(seconds=self._settings.archive_similar_freshness_ttl_s)
        return timedelta(seconds=self._settings.archive_freshness_ttl_s)

    def _ttl(self, method: str) -> timedelta | None:
        if not self._settings.archive_enabled:
            return None
        return self._freshness(method)

    async def artist_info(
        self, *, artist: str | None = None, mbid: str | None = None, lang: str | None = None
    ) -> tuple[LastfmArtist, LastfmResult]:
        params: dict[str, Any] = {}
        if mbid:
            params["mbid"] = mbid
        if artist:
            params["artist"] = artist
        if lang:
            params["lang"] = lang
        result = await self._call("artist.getinfo", params, max_age=self._ttl("artist.getinfo"))
        return parse_artist(result.body), result

    async def artist_top_tags(
        self, *, artist: str | None = None, mbid: str | None = None
    ) -> tuple[LastfmTopTags, LastfmResult]:
        params: dict[str, Any] = {}
        if mbid:
            params["mbid"] = mbid
        if artist:
            params["artist"] = artist
        result = await self._call(
            "artist.gettoptags", params, max_age=self._ttl("artist.gettoptags")
        )
        return parse_top_tags(result.body), result

    async def artist_similar(
        self, *, artist: str | None = None, mbid: str | None = None, limit: int = 20
    ) -> tuple[list[LastfmSimilarArtist], LastfmResult]:
        params: dict[str, Any] = {"limit": limit}
        if mbid:
            params["mbid"] = mbid
        if artist:
            params["artist"] = artist
        result = await self._call(
            "artist.getsimilar", params, max_age=self._ttl("artist.getsimilar")
        )
        return parse_similar(result.body), result

    async def album_info(
        self, *, artist: str | None = None, album: str | None = None, mbid: str | None = None
    ) -> tuple[LastfmAlbum, LastfmResult]:
        params: dict[str, Any] = {}
        if mbid:
            params["mbid"] = mbid
        if artist and album:
            params["artist"] = artist
            params["album"] = album
        result = await self._call("album.getinfo", params, max_age=self._ttl("album.getinfo"))
        return parse_album(result.body), result

    async def track_info(
        self, *, artist: str | None = None, track: str | None = None, mbid: str | None = None
    ) -> tuple[LastfmTrack, LastfmResult]:
        params: dict[str, Any] = {}
        if mbid:
            params["mbid"] = mbid
        if artist and track:
            params["artist"] = artist
            params["track"] = track
        result = await self._call("track.getinfo", params, max_age=self._ttl("track.getinfo"))
        return parse_track(result.body), result

    async def artist_search(
        self, query: str, *, limit: int = 5
    ) -> tuple[list[LastfmSearchResult], LastfmResult]:
        result = await self._call(
            "artist.search", {"artist": query, "limit": limit}, max_age=self._ttl("artist.search")
        )
        return parse_artist_search(result.body), result

    async def album_search(
        self, query: str, *, limit: int = 5
    ) -> tuple[list[LastfmSearchResult], LastfmResult]:
        result = await self._call(
            "album.search", {"album": query, "limit": limit}, max_age=self._ttl("album.search")
        )
        return parse_album_search(result.body), result

    async def probe(self) -> dict[str, Any]:
        """A single cheap call to confirm the key works. Used by ``/api/info``."""
        if not self._key:
            return {"configured": False, "ok": False, "error": "no_api_key"}
        try:
            await self.artist_search("cher", limit=1)
        except LastfmAuthError as exc:
            return {"configured": True, "ok": False, "error": exc.code, "message": exc.message}
        except Exception as exc:
            return {"configured": True, "ok": False, "error": type(exc).__name__}
        return {"configured": True, "ok": True}


def _always_sent_params() -> dict[str, str]:
    """Parameters the client sends on every request.

    They belong to the request identity as much as any caller-supplied argument
    does: ``autocorrect`` genuinely changes what Last.fm returns.
    """
    return {"autocorrect": "1"}


def _backoff_seconds(attempt: int, retry_after: str | None) -> float:
    if retry_after:
        try:
            return min(float(retry_after), 10.0)
        except ValueError:
            pass
    base: float = min(0.5 * (2 ** (attempt - 1)), 8.0)
    return base * (0.5 + random.random() / 2)  # jitter, per the ToS guidance


def contract_error(method: str, detail: str) -> LastfmContractError:
    return LastfmContractError(f"Unexpected Last.fm response for {method}", detail=detail)
