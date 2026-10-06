"""Archive byte accounting and read metrics.

Two distinct things live here, and keeping them separate is the point:

* **Byte accounting** measures how much Last.fm Data we have stored, against the
  Terms of Service cap. This is a database question, answered by ``measure``.
* **Read metrics** count how often the archive answered a lookup without a
  network call. This is *not* a database question: an archive hit is precisely
  the case where no HTTP attempt happened, so it must not be written into
  ``lastfm_request`` -- that table is the append-only log of attempts, and
  polluting it would make ``request_rows`` and the per-partition counts tell lies
  to exactly the analyses (rate limiting, failure hunting) that read them.

The byte cap is measured and surfaced, never silently enforced by deletion:
pruning the archive is always an explicit operator decision (ADR 0011).
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from metaedit.config import Settings
from metaedit.db.models import ArchiveStat, LastfmRequest, LastfmResponse
from metaedit.db.session import get_session_factory, init_engine

# How many recent read decisions to keep for the snapshot. Only used to report a
# recent-activity figure; unbounded growth would leak memory in a long-lived process.
_RECENT_READS = 500


@dataclass
class ReadMetrics:
    """Process-lifetime counts of archive hits and network fallbacks.

    Intentionally in memory. These are operational counters, not archive data:
    persisting them would recreate the very confusion between "we asked Last.fm"
    and "we answered locally" that this class exists to prevent.
    """

    hits: int = 0
    misses: int = 0
    recent: deque[tuple[float, bool]] = field(default_factory=lambda: deque(maxlen=_RECENT_READS))

    def record_hit(self, *, now: float | None = None) -> None:
        self.hits += 1
        self.recent.append((now if now is not None else time.time(), True))

    def record_miss(self, *, now: float | None = None) -> None:
        self.misses += 1
        self.recent.append((now if now is not None else time.time(), False))

    @property
    def decisions(self) -> int:
        return self.hits + self.misses

    @property
    def hit_ratio(self) -> float | None:
        """``None`` rather than 0 when nothing has been decided yet.

        A ratio of 0 would read as "the archive never helps", which is a claim we
        cannot make before the first lookup.
        """
        if self.decisions == 0:
            return None
        return self.hits / self.decisions

    def snapshot(self) -> dict[str, Any]:
        return {
            "hits": self.hits,
            "misses": self.misses,
            "decisions": self.decisions,
            "hit_ratio": None if self.hit_ratio is None else round(self.hit_ratio, 4),
            "trend": self._trend(),
            "recent_window": len(self.recent),
            "scope": "process",
            "note": (
                "In-memory counters since this process started. Archive reads are "
                "deliberately not written to lastfm_request, which logs HTTP attempts "
                "to Last.fm only."
            ),
        }

    def _trend(self) -> list[dict[str, Any]]:
        """A small ordered sample of recent decisions, oldest first."""
        return [{"at": round(at, 3), "hit": hit} for at, hit in list(self.recent)[-20:]]


_read_metrics = ReadMetrics()


def read_metrics() -> ReadMetrics:
    return _read_metrics


def reset_read_metrics() -> None:
    """Test hook: clear the process-wide counters."""
    global _read_metrics
    _read_metrics = ReadMetrics()


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
        "reads": _read_metrics.snapshot(),
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


async def count_stray_archive_reads(session: AsyncSession) -> int:
    """Rows in ``lastfm_request`` that are local reads, not Last.fm attempts.

    ``served_from_archive`` used to be written for archive hits, which put rows
    into the attempt log for requests that were never made. New writes cannot do
    that any more; this reports any historical rows so an operator can see (and
    optionally clear) them instead of silently trusting ``request_rows``.
    """
    return int(
        await session.scalar(
            select(func.count()).select_from(LastfmRequest).where(LastfmRequest.served_from_archive)
        )
        or 0
    )


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
