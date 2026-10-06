"""Server and capability information for the UI bootstrap."""

from __future__ import annotations

from typing import Annotated

import httpx
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from metaedit import __version__
from metaedit.adapters.jellyfin.client import JellyfinClient
from metaedit.config import Settings, get_settings
from metaedit.domain.errors import UpstreamError

router = APIRouter(tags=["info"])


class JellyfinInfo(BaseModel):
    reachable: bool
    server_name: str | None = None
    version: str | None = None
    key_configured: bool
    elevated: bool | None = None
    error: str | None = None


class LastfmInfo(BaseModel):
    configured: bool
    api_root: str


class ArchiveInfo(BaseModel):
    enabled: bool
    log_requests: bool
    cap_bytes: int


class InfoResponse(BaseModel):
    app_version: str
    jellyfin: JellyfinInfo
    lastfm: LastfmInfo
    archive: ArchiveInfo


@router.get("/info")
async def info(settings: Annotated[Settings, Depends(get_settings)]) -> InfoResponse:
    return InfoResponse(
        app_version=__version__,
        jellyfin=await _jellyfin_info(settings),
        lastfm=LastfmInfo(
            configured=bool(settings.lastfm_key()), api_root=settings.lastfm_base_url
        ),
        archive=ArchiveInfo(
            enabled=settings.archive_enabled,
            log_requests=settings.archive_log_requests,
            cap_bytes=settings.archive_soft_cap_bytes,
        ),
    )


async def _jellyfin_info(settings: Settings) -> JellyfinInfo:
    """Probe Jellyfin and report whether the configured key can write.

    ``POST /Items/{itemId}`` requires the ``RequiresElevation`` policy, so the UI needs
    to know up front whether the credential can write at all.

    This delegates the capability question to :meth:`JellyfinClient.can_write_metadata`
    rather than deciding it here. It previously sent its *own* request with a bare
    ``Authorization: <key>`` header and treated any non-200 from ``/Users/Me`` as
    "not elevated" -- so once the client was corrected to the MediaBrowser scheme, this
    endpoint went on reporting a working API key as unusable. Two copies of an
    authentication rule is one copy too many, and the stale one was the visible one.
    """
    key = settings.jellyfin_key()
    if not key:
        return JellyfinInfo(reachable=False, key_configured=False, error="no_api_key_configured")

    try:
        async with JellyfinClient(settings) as client:
            info = await client.system_info_public()
            allowed, reason = await client.can_write_metadata()
    except UpstreamError as exc:
        return JellyfinInfo(reachable=False, key_configured=True, error=exc.code)
    except httpx.HTTPError as exc:
        return JellyfinInfo(reachable=False, key_configured=True, error=type(exc).__name__)

    return JellyfinInfo(
        reachable=True,
        server_name=info.ServerName,
        version=info.Version,
        key_configured=True,
        elevated=allowed,
        error=None if allowed else (reason or "key_is_not_elevated"),
    )
