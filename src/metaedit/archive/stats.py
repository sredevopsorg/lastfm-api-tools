"""Archive byte accounting.

The Last.fm API Terms of Service cap stored Last.fm Data at 100 MB. Those bytes
are *measured and surfaced*, never silently deleted: pruning the archive is
always an explicit operator decision (``metaedit prune-raw``).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from metaedit.config import Settings
from metaedit.db.models import ArchiveStat, LastfmRequest, LastfmResponse
from metaedit.db.session import get_session_factory, init_engine


async def measure(session: AsyncSession, settings: Settings) -> dict[str, Any]:
    """Compute current archive footprint. One round trip per aggregate."""
    payload_bytes = await session.scalar(
        select(func.coalesce(func.sum(LastfmResponse.body_bytes), 0))
    )
    response_rows = await session.scalar(select(func.count()).select_from(LastfmResponse))
    observations = await session.scalar(
        select(func.coalesce(func.sum(LastfmResponse.observation_count), 0))
    )
    request_rows = await session.scalar(select(func.count()).select_from(LastfmRequest))
    oldest_request_at = await session.scalar(select(func.min(LastfmRequest.requested_at)))

    cap = settings.archive_soft_cap_bytes
    warn_at = settings.archive_warn_bytes
    refuse_at = settings.archive_refuse_bytes
    used = int(payload_bytes or 0)

    return {
        "payload_bytes": used,
        "cap_bytes": cap,
        "warn_bytes": warn_at,
        "refuse_bytes": refuse_at,
        "used_ratio": round(used / cap, 6) if cap else 0.0,
        "headroom_bytes": max(cap - used, 0),
        "state": _state(used, warn_at, refuse_at),
        "response_rows": int(response_rows or 0),
        "observations": int(observations or 0),
        "request_rows": int(request_rows or 0),
        "oldest_request_at": oldest_request_at.isoformat() if oldest_request_at else None,
    }


def _state(used: int, warn_at: int, refuse_at: int) -> str:
    if used >= refuse_at:
        return "cap_reached"
    if used >= warn_at:
        return "warning"
    return "ok"


async def collect_stats(settings: Settings) -> dict[str, Any]:
    """Standalone entry point used by the CLI."""
    init_engine(settings)
    factory = get_session_factory()
    async with factory() as session:
        stats = await measure(session, settings)
        session.add(
            ArchiveStat(
                payload_bytes=stats["payload_bytes"],
                request_rows=stats["request_rows"],
                response_rows=stats["response_rows"],
                observations=stats["observations"],
                cap_bytes=stats["cap_bytes"],
            )
        )
        await session.commit()
    return stats


async def can_store_new_payload(
    session: AsyncSession, settings: Settings, incoming_bytes: int
) -> bool:
    """Whether a *new distinct* payload may be written under the configured cap.

    Repeat observations of an already-stored body cost nothing and are always
    allowed, so the cap only ever blocks genuinely new data.
    """
    if not settings.archive_enabled:
        return False
    current = await session.scalar(select(func.coalesce(func.sum(LastfmResponse.body_bytes), 0)))
    used = int(current or 0)
    if used < settings.archive_refuse_bytes:
        return True
    # Once refusing, allow a payload if we are still below the hard cap.
    return used + incoming_bytes <= settings.archive_soft_cap_bytes


async def partition_usage(session: AsyncSession) -> list[dict[str, Any]]:
    """Per-partition row counts, for the operator-facing stats endpoint."""
    result = await session.execute(
        text(
            "select c.relname as name, "
            "  (select count(*) from lastfm_request r "
            "   where r.tableoid = c.oid) as rows "
            "from pg_class c "
            "join pg_inherits i on i.inhrelid = c.oid "
            "join pg_class p on p.oid = i.inhparent "
            "where p.relname = 'lastfm_request' "
            "order by c.relname"
        )
    )
    return [{"name": row[0], "rows": int(row[1])} for row in result]
