"""``metaedit`` command line: operational tasks the HTTP API should not own.

Everything destructive is explicit and confirmed; nothing here runs on a timer.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence

from metaedit import __version__
from metaedit.config import get_settings
from metaedit.db.partitions import ensure_partitions_sync, prune_candidates
from metaedit.db.session import sync_connection


def _cmd_partitions(args: argparse.Namespace) -> int:
    settings = get_settings()
    with sync_connection(settings.database_url) as engine, engine.begin() as conn:
        created = ensure_partitions_sync(conn, months_ahead=args.months)
    for name in created:
        print(name)
    return 0


def _cmd_prune_raw(args: argparse.Namespace) -> int:
    """Report (and only on --yes, drop) archive partitions older than N days.

    Exits 0 in both report and drop modes. Reporting is what a dry run is *for*,
    so finding candidates is success, not failure -- a non-zero exit would make
    every shell `&&` chain and CI step treat a working retention check as broken.
    """
    settings = get_settings()
    candidates = prune_candidates(keep_days=args.keep_days)
    existing = _existing_partitions(settings.database_url)
    present = [name for name in candidates if name in existing]
    if not present:
        print("nothing to prune")
        return 0

    print("\n".join(present))
    if not args.yes:
        print(
            f"\n{len(present)} partition(s) would be dropped. "
            "This deletes archived Last.fm history irreversibly. Re-run with --yes to proceed.",
            file=sys.stderr,
        )
        return 0

    with sync_connection(settings.database_url) as engine, engine.begin() as conn:
        for name in present:
            conn.exec_driver_sql(f'DROP TABLE IF EXISTS "{name}"')
    print(f"dropped {len(present)} partition(s)")
    return 0


def _existing_partitions(database_url: str) -> set[str]:
    from sqlalchemy import text

    with sync_connection(database_url) as engine, engine.connect() as conn:
        rows = conn.execute(
            text(
                "select c.relname from pg_class c "
                "join pg_inherits i on i.inhrelid = c.oid "
                "join pg_class p on p.oid = i.inhparent "
                "where p.relname = 'lastfm_request'"
            )
        )
        return {row[0] for row in rows}


def _cmd_archive_stats(args: argparse.Namespace) -> int:
    from metaedit.archive.stats import collect_stats

    settings = get_settings()
    stats = asyncio.run(collect_stats(settings))
    print(json.dumps(stats, indent=2, default=str))
    return 0


def _cmd_reindex(args: argparse.Namespace) -> int:
    from metaedit.archive.reindex import reindex

    settings = get_settings()
    report = asyncio.run(reindex(settings, dry_run=args.dry_run, since=args.since, only=args.only))
    print(json.dumps(report, indent=2, default=str))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="metaedit", description=__doc__)
    parser.add_argument("--version", action="version", version=f"metaedit {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    partitions = sub.add_parser("partitions", help="pre-create monthly archive partitions")
    partitions.add_argument(
        "--months", type=int, default=None, help="months ahead (default: config)"
    )
    partitions.set_defaults(func=_cmd_partitions)

    stats = sub.add_parser("archive-stats", help="report archive size against the ToS cap")
    stats.set_defaults(func=_cmd_archive_stats)

    reindex = sub.add_parser(
        "reindex", help="rebuild derived tables from the raw archive (no network access)"
    )
    reindex.add_argument("--dry-run", action="store_true", help="report the diff, write nothing")
    reindex.add_argument(
        "--since", default=None, help="only responses first seen after this ISO date"
    )
    reindex.add_argument(
        "--only", default=None, choices=["artist", "album", "track", "tag", "similar", "alias"]
    )
    reindex.set_defaults(func=_cmd_reindex)

    prune = sub.add_parser("prune-raw", help="report archive partitions older than N days")
    prune.add_argument("--keep-days", type=int, required=True)
    prune.add_argument("--yes", action="store_true", help="actually drop the partitions")
    prune.set_defaults(func=_cmd_prune_raw)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "partitions" and args.months is None:
        args.months = get_settings().archive_partition_months_ahead
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
