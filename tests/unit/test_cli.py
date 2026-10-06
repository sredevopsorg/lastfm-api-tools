"""CLI command behaviour.

The exit codes matter as much as the output: these commands are meant to be
called from shell `&&` chains and CI steps, so "did the operation succeed" must
not be confused with "was there something to report".
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterator
from datetime import UTC, date, datetime
from unittest import mock

import pytest

from metaedit import cli
from metaedit.db.partitions import partition_name

PARTITION = "lastfm_request_2026_01"


@pytest.fixture
def prune_env() -> Iterator[None]:
    """Stub out configuration and the database so only the decision logic runs."""
    with (
        mock.patch.object(cli, "get_settings") as settings,
        mock.patch.object(cli, "prune_candidates", return_value=[PARTITION]),
        mock.patch.object(cli, "_existing_partitions", return_value={PARTITION}),
        mock.patch.object(cli, "sync_connection"),
    ):
        settings.return_value = mock.Mock(database_url="postgresql+psycopg://unused/unused")
        yield


def _run(capsys: pytest.CaptureFixture[str], *, yes: bool) -> tuple[int, str]:
    code = cli._cmd_prune_raw(argparse.Namespace(keep_days=365, yes=yes))
    captured = capsys.readouterr()
    return code, captured.out + captured.err


def test_dry_run_report_exits_zero(prune_env: None, capsys: pytest.CaptureFixture[str]) -> None:
    """Reporting candidates is what a dry run is for, so it is not a failure."""
    code, output = _run(capsys, yes=False)
    assert code == 0, "a working retention check must not look like a crash to a caller"
    assert PARTITION in output
    assert "Re-run with --yes" in output


def test_dry_run_does_not_drop_anything(
    prune_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    with mock.patch.object(cli, "sync_connection") as connect:
        code, _ = _run(capsys, yes=False)
    assert code == 0
    assert not connect.called, "a dry run must not open a database connection"


def test_confirmed_drop_reports_and_exits_zero(
    prune_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    code, output = _run(capsys, yes=True)
    assert code == 0
    assert "dropped 1 partition(s)" in output


def test_nothing_to_prune_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        mock.patch.object(cli, "get_settings") as settings,
        mock.patch.object(cli, "prune_candidates", return_value=[]),
        mock.patch.object(cli, "_existing_partitions", return_value=set()),
    ):
        settings.return_value = mock.Mock(database_url="postgresql+psycopg://unused/unused")
        code = cli._cmd_prune_raw(argparse.Namespace(keep_days=0, yes=False))
    assert code == 0
    assert "nothing to prune" in capsys.readouterr().out


def test_candidates_absent_from_the_database_are_ignored(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A retention window can reach back before the archive existed."""
    with (
        mock.patch.object(cli, "get_settings") as settings,
        mock.patch.object(cli, "prune_candidates", return_value=[PARTITION]),
        mock.patch.object(cli, "_existing_partitions", return_value=set()),
    ):
        settings.return_value = mock.Mock(database_url="postgresql+psycopg://unused/unused")
        code = cli._cmd_prune_raw(argparse.Namespace(keep_days=3650, yes=True))
    assert code == 0
    assert "nothing to prune" in capsys.readouterr().out


def test_negative_retention_is_rejected_before_any_work() -> None:
    with pytest.raises(ValueError, match="must not be negative"):
        cli._cmd_prune_raw(argparse.Namespace(keep_days=-1, yes=False))


def test_reindex_prints_a_json_report(capsys: pytest.CaptureFixture[str]) -> None:
    """The report is machine-readable: this is a command an operator scripts."""
    from metaedit.archive.reindex import ReindexReport

    report = ReindexReport(dry_run=True, artists=2, tag_edges=5)
    with (
        mock.patch.object(cli, "get_settings") as settings,
        mock.patch(
            "metaedit.archive.reindex.reindex_with_settings",
            new=mock.AsyncMock(return_value=report),
        ),
    ):
        settings.return_value = mock.Mock(database_url="postgresql+psycopg://unused/unused")
        code = cli._cmd_reindex(argparse.Namespace(dry_run=True, since=None, only=None))

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["dry_run"] is True
    assert payload["counts"]["artists"] == 2


def test_reindex_refusal_exits_non_zero_without_a_traceback(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A refused derivation is an operator error, not a crash."""
    from metaedit.archive.reindex import ReindexError

    with (
        mock.patch.object(cli, "get_settings") as settings,
        mock.patch(
            "metaedit.archive.reindex.reindex_with_settings",
            new=mock.AsyncMock(side_effect=ReindexError("dangling response reference")),
        ),
    ):
        settings.return_value = mock.Mock(database_url="postgresql+psycopg://unused/unused")
        code = cli._cmd_reindex(argparse.Namespace(dry_run=False, since=None, only=None))

    captured = capsys.readouterr()
    assert code == 2, "distinguishable from success and from an unexpected crash"
    assert "dangling response reference" in captured.err


def test_parser_requires_a_subcommand() -> None:
    parser = cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([])


def test_parser_accepts_the_documented_commands() -> None:
    parser = cli.build_parser()
    for argv in (
        ["partitions", "--months", "6"],
        ["archive-stats"],
        ["prune-raw", "--keep-days", "365", "--yes"],
        ["reindex", "--dry-run", "--only", "artist"],
    ):
        assert parser.parse_args(argv)


def test_parser_rejects_an_unknown_reindex_target() -> None:
    parser = cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["reindex", "--only", "not-a-table"])


def test_partitions_months_is_resolved_from_settings_when_omitted() -> None:
    """`--months` is optional; omitting it must use the configured window.

    This is the code path an operator actually takes, and it is the one that could
    silently default to zero and create only the current month's partition.
    """
    parser = cli.build_parser()
    assert parser.parse_args(["partitions"]).months is None, "resolved in main(), not the parser"

    with (
        mock.patch.object(cli, "get_settings") as settings,
        mock.patch.object(cli, "ensure_partitions_sync", return_value=["p"]) as ensure,
        mock.patch.object(cli, "sync_connection") as connect,
    ):
        settings.return_value = mock.Mock(
            database_url="postgresql+psycopg://unused/unused",
            archive_partition_months_ahead=5,
        )
        connect.return_value.__enter__.return_value.begin.return_value.__enter__.return_value = (
            mock.Mock()
        )
        code = cli.main(["partitions"])

    assert code == 0
    assert ensure.call_args.kwargs["months_ahead"] == 5, "must come from settings, not 0"


def test_partitions_months_flag_overrides_settings() -> None:
    with (
        mock.patch.object(cli, "get_settings") as settings,
        mock.patch.object(cli, "ensure_partitions_sync", return_value=["p"]) as ensure,
        mock.patch.object(cli, "sync_connection") as connect,
    ):
        settings.return_value = mock.Mock(
            database_url="postgresql+psycopg://unused/unused",
            archive_partition_months_ahead=5,
        )
        connect.return_value.__enter__.return_value.begin.return_value.__enter__.return_value = (
            mock.Mock()
        )
        cli.main(["partitions", "--months", "9"])

    assert ensure.call_args.kwargs["months_ahead"] == 9


def test_prune_window_is_monthly_and_ordered() -> None:
    """The names reported are real partitions, oldest first."""
    now = datetime(2026, 7, 15, tzinfo=UTC)
    candidates = cli.prune_candidates(keep_days=365, now=now)
    assert candidates == sorted(candidates), "oldest first, so the report reads chronologically"
    assert candidates[0] == partition_name(date(2025, 8, 1))
    assert all(name.startswith("lastfm_request_") for name in candidates)
