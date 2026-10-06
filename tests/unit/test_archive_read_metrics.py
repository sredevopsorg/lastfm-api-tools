"""Read metrics for the archive.

These counters replaced a bug, so they are worth testing on their own: archive
hits used to be written into ``lastfm_request``, which is the append-only log of
HTTP attempts to Last.fm. A hit is precisely the case where no attempt happened,
so logging it there corrupted ``request_rows`` and the partition counts.
"""

from __future__ import annotations

import pytest

from metaedit.archive.stats import ReadMetrics, read_metrics, reset_read_metrics


def test_empty_metrics_report_no_ratio() -> None:
    """A ratio of 0 would claim "the archive never helps", before any lookup."""
    metrics = ReadMetrics()
    assert metrics.hit_ratio is None
    assert metrics.decisions == 0
    assert metrics.snapshot()["hit_ratio"] is None


def test_hits_and_misses_accumulate() -> None:
    metrics = ReadMetrics()
    metrics.record_hit()
    metrics.record_hit()
    metrics.record_miss()
    assert metrics.hits == 2
    assert metrics.misses == 1
    assert metrics.decisions == 3
    assert metrics.hit_ratio == pytest.approx(2 / 3)


def test_all_hits_is_a_ratio_of_one() -> None:
    metrics = ReadMetrics()
    for _ in range(4):
        metrics.record_hit()
    assert metrics.hit_ratio == 1.0


def test_all_misses_is_a_ratio_of_zero() -> None:
    metrics = ReadMetrics()
    metrics.record_miss()
    assert metrics.hit_ratio == 0.0
    assert metrics.hit_ratio is not None, "0.0 is a real measurement, not 'unknown'"


def test_recent_window_is_bounded() -> None:
    """A long-lived process must not accumulate read history without limit."""
    metrics = ReadMetrics()
    for _ in range(5000):
        metrics.record_hit()
    assert metrics.hits == 5000, "the counters are exact"
    assert len(metrics.recent) == 500, "but the sample is capped"


def test_trend_is_ordered_and_describes_hits() -> None:
    metrics = ReadMetrics()
    metrics.record_hit(now=1.0)
    metrics.record_miss(now=2.0)
    trend = metrics.snapshot()["trend"]
    assert [entry["at"] for entry in trend] == [1.0, 2.0], "oldest first"
    assert [entry["hit"] for entry in trend] == [True, False]


def test_snapshot_is_self_describing_about_scope() -> None:
    """The scope must be explicit, or a reader will assume these are persisted."""
    snapshot = ReadMetrics().snapshot()
    assert snapshot["scope"] == "process"
    assert "lastfm_request" in snapshot["note"]
    assert snapshot["recent_window"] == 0


def test_process_wide_metrics_are_shared() -> None:
    reset_read_metrics()
    read_metrics().record_hit()
    assert read_metrics().hits == 1, "callers must see the same counters"


def test_reset_clears_the_process_counters() -> None:
    read_metrics().record_hit()
    reset_read_metrics()
    assert read_metrics().hits == 0
    assert read_metrics().hit_ratio is None
