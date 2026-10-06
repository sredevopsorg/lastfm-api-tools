"""Partition window arithmetic.

Getting these wrong is either an outage (a write with no partition to hold it) or
data loss (pruning a month that still contains recent observations), so the
boundaries are tested explicitly rather than reasoned about.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from metaedit.db.partitions import (
    add_months,
    ensure_partitions_sync,
    month_start,
    next_maintenance_at,
    partition_name,
    prune_candidates,
)


@pytest.mark.parametrize(
    ("start", "months", "expected"),
    [
        (date(2026, 1, 15), 1, date(2026, 2, 1)),
        (date(2026, 12, 1), 1, date(2027, 1, 1)),
        (date(2026, 1, 1), 12, date(2027, 1, 1)),
        (date(2026, 3, 31), 13, date(2027, 4, 1)),
        (date(2026, 5, 1), -1, date(2026, 4, 1)),
        (date(2026, 1, 1), -1, date(2025, 12, 1)),
        (date(2026, 1, 1), 0, date(2026, 1, 1)),
    ],
)
def test_add_months(start: date, months: int, expected: date) -> None:
    assert add_months(start, months) == expected


def test_month_start_truncates_to_the_first() -> None:
    assert month_start(date(2026, 7, 29)) == date(2026, 7, 1)


def test_partition_name_is_monthly_and_sortable() -> None:
    assert partition_name(date(2026, 7, 1)) == "lastfm_request_2026_07"
    names = [partition_name(date(2026, month, 1)) for month in (1, 10, 2, 11, 12)]
    assert sorted(names) == [
        "lastfm_request_2026_01",
        "lastfm_request_2026_02",
        "lastfm_request_2026_10",
        "lastfm_request_2026_11",
        "lastfm_request_2026_12",
    ]


class _Recorder:
    """Minimal Connection double: records the DDL it is asked to run."""

    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement: object) -> None:
        self.statements.append(str(statement))


def test_ensure_partitions_covers_the_current_month_and_ahead() -> None:
    conn = _Recorder()
    created = ensure_partitions_sync(
        conn,  # type: ignore[arg-type]
        months_ahead=2,
        today=date(2026, 7, 15),
    )
    assert created == [
        "lastfm_request_2026_07",
        "lastfm_request_2026_08",
        "lastfm_request_2026_09",
    ]


def test_ensure_partitions_is_idempotent_by_construction() -> None:
    """Every partition statement uses IF NOT EXISTS, so re-running is safe."""
    conn = _Recorder()
    ensure_partitions_sync(conn, months_ahead=1, today=date(2026, 7, 15))  # type: ignore[arg-type]
    creates = [s for s in conn.statements if "CREATE TABLE" in s]
    assert creates and all("IF NOT EXISTS" in statement for statement in creates)


def test_ensure_partitions_sets_a_lock_timeout() -> None:
    """A blocked PARTITION OF must fail fast, not hang a request."""
    conn = _Recorder()
    ensure_partitions_sync(conn, months_ahead=0, today=date(2026, 7, 15))  # type: ignore[arg-type]
    assert any("lock_timeout" in statement for statement in conn.statements)


def test_partition_bounds_are_half_open() -> None:
    """Ranges must not overlap, or a row could land in two partitions."""
    conn = _Recorder()
    ensure_partitions_sync(conn, months_ahead=1, today=date(2026, 7, 15))  # type: ignore[arg-type]
    creates = [s for s in conn.statements if "CREATE TABLE" in s]
    assert "FROM ('2026-07-01') TO ('2026-08-01')" in creates[0]
    assert "FROM ('2026-08-01') TO ('2026-09-01')" in creates[1]


def test_next_maintenance_is_the_first_of_next_month() -> None:
    assert next_maintenance_at(now=datetime(2026, 7, 15, 12, 0, tzinfo=UTC)) == datetime(
        2026, 8, 1, tzinfo=UTC
    )
    assert next_maintenance_at(now=datetime(2026, 12, 31, 23, 59, tzinfo=UTC)) == datetime(
        2027, 1, 1, tzinfo=UTC
    )


def test_prune_candidates_never_includes_the_current_month() -> None:
    """The current month always holds data we must not delete."""
    now = datetime(2026, 7, 15, tzinfo=UTC)
    assert prune_candidates(keep_days=0, now=now) == []
    for keep_days in (0, 1, 7, 30, 365):
        candidates = prune_candidates(keep_days=keep_days, now=now)
        assert "lastfm_request_2026_07" not in candidates


def test_prune_candidates_excludes_the_cutoff_month_containing_today() -> None:
    """The month that contains any part of the retention window is kept whole.

    With a 7-day retention from 15 July the cutoff is 8 July, inside July, so
    nothing is a candidate: pruning is monthly, never mid-month.
    """
    now = datetime(2026, 7, 15, tzinfo=UTC)
    assert prune_candidates(keep_days=7, now=now) == []


def test_prune_candidates_rounds_the_shallow_window_in_the_safe_direction() -> None:
    """With a 30-day retention from 15 July the cutoff is 15 June.

    June and May are both within reach of the retention window, and a monthly
    prune cannot drop part of a month, so nothing is listed. At 60 days the cutoff
    moves to 16 May and June becomes wholly older than it.
    """
    now = datetime(2026, 7, 15, tzinfo=UTC)
    assert prune_candidates(keep_days=30, now=now) == []
    assert prune_candidates(keep_days=60, now=now) == ["lastfm_request_2026_06"]


def test_prune_candidates_lists_fully_elapsed_months() -> None:
    now = datetime(2026, 7, 15, tzinfo=UTC)
    # Cutoff is 2025-07-15, inside July 2025. July 2025 and the month after it
    # are retained (rounding to the safe side), so the list ends at June 2026.
    candidates = prune_candidates(keep_days=365, now=now)
    assert candidates[0] == "lastfm_request_2025_08"
    assert candidates[-1] == "lastfm_request_2026_06"
    assert len(candidates) == 11, "August 2025 through June 2026 inclusive"
    assert "lastfm_request_2026_07" not in candidates, "the current month is never a candidate"
    assert "lastfm_request_2025_07" not in candidates, "the cutoff month is retained"

    # Every entry is a distinct, well-formed, non-empty name.
    assert len(set(candidates)) == len(candidates)
    assert all(name.startswith("lastfm_request_") for name in candidates)


def test_prune_candidates_walks_back_far_enough() -> None:
    """Retention must not be capped by a fixed epoch."""
    now = datetime(2026, 7, 15, tzinfo=UTC)
    candidates = prune_candidates(keep_days=365 * 25, now=now)
    assert len(candidates) > 250
    assert candidates[0].startswith("lastfm_request_2")


def test_prune_candidates_rejects_a_negative_retention() -> None:
    with pytest.raises(ValueError, match="must not be negative"):
        prune_candidates(keep_days=-1)
