"""Server and capability information for the UI bootstrap."""

from __future__ import annotations

from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from metaedit import __version__
from metaedit.config import Settings, get_settings

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
    """Probe Jellyfin and report whether our key is usable at all.

    ``POST /Items/{itemId}`` requires the ``RequiresElevation`` policy, so the
    UI needs to know up front whether the configured key can even write.
    """
    key = settings.jellyfin_key()
    if not key:
        return JellyfinInfo(reachable=False, key_configured=False, error="no_api_key_configured")

    headers = {"Authorization": key}
    try:
        async with httpx.AsyncClient(timeout=min(settings.jellyfin_timeout_s, 5.0)) as client:
            public = await client.get(f"{settings.jellyfin_base_url}/System/Info/Public")
            if public.status_code != 200:
                return JellyfinInfo(
                    reachable=False,
                    key_configured=True,
                    error=f"http_{public.status_code}",
                )
            payload: dict[str, Any] = public.json()

            me = await client.get(f"{settings.jellyfin_base_url}/Users/Me", headers=headers)
            elevated = me.status_code == 200 and bool(
                me.json().get("Policy", {}).get("IsAdministrator")
            )
    except httpx.HTTPError as exc:
        return JellyfinInfo(reachable=False, key_configured=True, error=type(exc).__name__)

    return JellyfinInfo(
        reachable=True,
        server_name=payload.get("ServerName"),
        version=payload.get("Version"),
        key_configured=True,
        elevated=elevated,
        error=None if elevated else "key_is_not_elevated",
    )
