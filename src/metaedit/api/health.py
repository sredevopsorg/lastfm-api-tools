"""Health endpoints.

``/api/health`` is liveness and must never touch Postgres, Jellyfin or Last.fm:
it backs the container HEALTHCHECK and must answer while dependencies are down.
``/api/health/ready`` is readiness and reports each dependency independently.
"""

from __future__ import annotations

from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from metaedit.config import Settings, get_settings
from metaedit.db.session import get_session

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, str]:
    """Dependency-free liveness probe."""
    return {"status": "ok"}


@router.get("/health/ready")
async def readiness(
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """Readiness: report each dependency independently so partial failures are visible."""
    checks: dict[str, Any] = {}

    try:
        row = await session.execute(text("select version()"))
        checks["postgres"] = {"ok": True, "version": row.scalar_one()}
    except Exception as exc:
        checks["postgres"] = {"ok": False, "error": type(exc).__name__}

    checks["jellyfin"] = await _probe_jellyfin(settings)
    checks["lastfm"] = {
        "configured": bool(settings.lastfm_key()),
        "note": "Last.fm is probed on demand; not part of readiness",
    }
    ready = bool(checks["postgres"]["ok"] and checks["jellyfin"]["ok"])
    return {"status": "ready" if ready else "degraded", "checks": checks}


async def _probe_jellyfin(settings: Settings) -> dict[str, Any]:
    if not settings.jellyfin_key():
        return {"ok": False, "error": "no_api_key_configured"}
    url = f"{settings.jellyfin_base_url}/System/Info/Public"
    try:
        async with httpx.AsyncClient(timeout=min(settings.jellyfin_timeout_s, 5.0)) as client:
            response = await client.get(url)
        if response.status_code != 200:
            return {"ok": False, "error": f"http_{response.status_code}"}
        payload = response.json()
        return {
            "ok": True,
            "server_name": payload.get("ServerName"),
            "version": payload.get("Version"),
        }
    except httpx.HTTPError as exc:
        return {"ok": False, "error": type(exc).__name__}
