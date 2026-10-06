"""Monthly partition management for the append-only ``lastfm_request`` log.

Writes must never fail because a partition is missing, so partitions are
pre-created ahead of time (default: three months) and a small scheduled job
keeps the window rolling forward.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncConnection

PARENT_TABLE = "lastfm_request"


def month_start(value: date) -> date:
    return value.replace(day=1)


def add_months(value: date, months: int) -> date:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    return date(year, month, 1)


def partition_name(month: date) -> str:
    return f"{PARENT_TABLE}_{month:%Y_%m}"


def month_range(month: date) -> tuple[date, date]:
    return month, add_months(month, 1)


def _ddl_statements(month: date) -> list[str]:
    """Partition DDL, guarded so it can never block the API indefinitely.

    ``CREATE TABLE ... PARTITION OF`` takes an ACCESS EXCLUSIVE lock on the
    parent. A short ``lock_timeout`` turns a pathological wait into a fast,
    retryable error instead of a hung request.
    """
    start, end = month_range(month)
    return [
        "SET LOCAL lock_timeout = '3s'",
        (
            f"CREATE TABLE IF NOT EXISTS {partition_name(month)} "
            f"PARTITION OF {PARENT_TABLE} "
            f"FOR VALUES FROM ('{start.isoformat()}') TO ('{end.isoformat()}')"
        ),
    ]


def ensure_partitions_sync(
    conn: Connection, *, months_ahead: int = 3, today: date | None = None
) -> list[str]:
    """Create partitions from the current month through ``months_ahead``.

    Must run inside a transaction (DDL is transactional in Postgres and
    ``SET LOCAL`` only lives for one).
    """
    created: list[str] = []
    base = month_start(today or datetime.now(UTC).date())
    for offset in range(0, months_ahead + 1):
        month = add_months(base, offset)
        for statement in _ddl_statements(month):
            conn.execute(text(statement))
        created.append(partition_name(month))
    return created


async def ensure_partitions(conn: AsyncConnection, *, months_ahead: int = 3) -> list[str]:
    created: list[str] = []
    base = month_start(datetime.now(UTC).date())
    for offset in range(0, months_ahead + 1):
        month = add_months(base, offset)
        for statement in _ddl_statements(month):
            await conn.execute(text(statement))
        created.append(partition_name(month))
    return created


def next_maintenance_at(*, now: datetime | None = None) -> datetime:
    """When to next roll the partition window forward (first of next month)."""
    current = now or datetime.now(UTC)
    return datetime.combine(
        add_months(month_start(current.date()), 1), datetime.min.time(), tzinfo=UTC
    )


def prune_candidates(*, keep_days: int, now: datetime | None = None) -> list[str]:
    """Partitions wholly older than ``keep_days`` -- reported by ``metaedit prune-raw``.

    Walks forward from the oldest droppable month rather than from a fixed epoch,
    so the window is correct for any retention period. The cutoff month *and the
    month before it* are retained: the cutoff month holds at least one retained
    day, and because pruning is monthly rather than mid-month, the rule errs by up
    to one month on the safe side. The current month is never a candidate, since
    it is always at or after the cutoff month.

    Deliberately never called automatically: deleting archive data is always an
    explicit operator decision.
    """
    if keep_days < 0:
        msg = "keep_days must not be negative"
        raise ValueError(msg)
    if keep_days == 0:
        # Cutoff is now, so the cutoff month is the current month: nothing is
        # wholly in the past yet.
        return []
    current = now or datetime.now(UTC)
    first_month = month_start(current.date())
    cutoff_month = month_start((current - timedelta(days=keep_days)).date())
    # The cutoff month itself is retained, so start one month later.
    month = add_months(cutoff_month, 1)
    names: list[str] = []
    while month < first_month:
        names.append(partition_name(month))
        month = add_months(month, 1)
    return names
