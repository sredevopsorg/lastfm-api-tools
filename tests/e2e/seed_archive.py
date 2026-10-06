"""Seed the e2e database with one archived Last.fm artist.

Separate from the stub Jellyfin because the two halves of the journey come from
different places: Jellyfin supplies the item to edit, and our archive supplies what to
propose. Seeding through the real store and reindex keeps the fixture honest -- the
derived layer the UI reads is produced by the same code path production uses, not by
inserting rows that happen to satisfy the queries.
"""

from __future__ import annotations

import asyncio
import sys

from metaedit.archive.reindex import reindex
from metaedit.archive.store import ArchiveStore, Observation
from metaedit.config import get_settings
from metaedit.db.partitions import ensure_partitions
from metaedit.db.session import get_session_factory, init_engine

ARTIST = {
    "artist": {
        "name": "Radiohead",
        "mbid": "a74b1b7f-71a5-4011-9441-d0b5e4122711",
        "url": "https://www.last.fm/music/Radiohead",
        "stats": {"listeners": "5000000", "playcount": "100000000"},
        "tags": {"tag": [{"name": "rock"}, {"name": "alternative"}]},
        # Deliberately short: the Overview policy has a minimum length, so a thin
        # biography must be withheld rather than written. The e2e journey asserts on the
        # fields that *are* proposed, so this keeps the test about the mechanism.
        "bio": {
            "summary": (
                "Radiohead are an English rock band formed in Abingdon, Oxfordshire, in "
                "1985. They are known for their experimental approach and for pushing "
                "against the conventions of the music industry."
            ),
            "published": "Thu, 13 Mar 2008",
        },
    }
}

TOP_TAGS = {
    "toptags": {
        "@attr": {"artist": "Radiohead"},
        "tag": [
            {"name": "rock", "count": 100},
            {"name": "alternative", "count": 54},
            {"name": "art rock", "count": 30},
            {"name": "electronic", "count": 11},
        ],
    }
}


async def main() -> int:
    settings = get_settings()
    engine = init_engine(settings)
    factory = get_session_factory()
    async with factory() as session:
        await ensure_partitions(await session.connection(), months_ahead=1)
        store = ArchiveStore(session, settings)
        await store.record(
            Observation(
                method="artist.getinfo",
                params={"artist": "Radiohead", "autocorrect": "1"},
                http_status=200,
                duration_ms=12,
                body=ARTIST,
                user_agent="e2e",
            )
        )
        # The popularity source, without which every tag count is null.
        await store.record(
            Observation(
                method="artist.gettoptags",
                params={"artist": "Radiohead", "autocorrect": "1"},
                http_status=200,
                duration_ms=9,
                body=TOP_TAGS,
                user_agent="e2e",
            )
        )
        await session.commit()
        report = await reindex(session)
        await session.commit()
    await engine.dispose()

    counts = report.as_dict()["counts"]
    print(f"seeded: {counts}", file=sys.stderr)
    return 0 if counts["artists"] >= 1 and counts["unexpected_shapes"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
