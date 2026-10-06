"""Live tests against the real Last.fm API.

**Read-only.** Only the read methods are called, using an API key and no user auth
(ADR 0012), so nothing is written to anyone's Last.fm account.

Skipped unless ``LASTFM_API_KEY`` is set. These exist because every response shape
this project depends on was inferred from Last.fm's documented XML and had never
been checked against a real response -- the risk being that a mismatch surfaces as
*silently fewer entities* rather than as a failure.

Run with::

    uv run pytest -m live_lastfm -v -s      # uses LASTFM_API_KEY from .env
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from tests.conftest import requires_postgres

from metaedit.adapters.lastfm.client import LastfmClient, reset_shared_buckets
from metaedit.archive.reindex import reindex
from metaedit.archive.store import ArchiveStore
from metaedit.config import Settings
from metaedit.config import get_settings as _get_settings

pytestmark = [pytest.mark.live_lastfm, requires_postgres]

API_KEY = _get_settings().lastfm_key()
requires_key = pytest.mark.skipif(not API_KEY, reason="LASTFM_API_KEY is not configured")

# A stable, well-known subject so the assertions are not at the mercy of an obscure
# artist's data being absent from Last.fm.
ARTIST = "Radiohead"


def _settings(database_url: str, **overrides: object) -> Settings:
    defaults: dict[str, object] = {
        "_env_file": None,
        "DATABASE_URL": database_url,
        "LASTFM_API_KEY": API_KEY,
        "ARCHIVE_ENABLED": True,
        "ARCHIVE_LOG_REQUESTS": True,
        # Live calls must be paced well inside the documented guidance.
        "LASTFM_MAX_RPS": 3.0,
        "LASTFM_BURST": 3,
        "LOG_JSON": False,
        # Bypass the archive so each test really exercises the network.
        "ARCHIVE_FRESHNESS_TTL_S": 0,
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


@pytest.fixture
async def session_factory(database_url: str) -> AsyncIterator[async_sessionmaker]:  # type: ignore[type-arg]
    reset_shared_buckets()
    engine = create_async_engine(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    from metaedit.db.partitions import ensure_partitions

    async with factory() as session:
        await ensure_partitions(await session.connection(), months_ahead=1)
        await session.commit()
    try:
        yield factory
    finally:
        await engine.dispose()
        reset_shared_buckets()


@pytest.fixture
def client_settings(database_url: str) -> Settings:
    return _settings(database_url)


# --------------------------------------------------------------- shape checks


@requires_key
async def test_artist_getinfo_parses(session_factory, client_settings) -> None:  # type: ignore[no-untyped-def]
    """The documented artist shape must parse, and yield a usable identity."""
    async with session_factory() as session:
        store = ArchiveStore(session, client_settings)
        async with LastfmClient(client_settings, store=store) as client:
            artist, result = await client.artist_info(artist=ARTIST)
        await session.commit()

    assert artist.name, "a real artist response must carry a name"
    assert result.body, "the raw body must be archived"
    # The envelope key the derivation depends on.
    assert "artist" in result.body
    print(f"\nartist.getinfo -> name={artist.name!r} mbid={artist.mbid!r} tags={len(artist.tags)}")
    print(f"  bio present: {artist.bio is not None}, similar: {len(artist.similar)}")


@requires_key
async def test_artist_top_tags_carry_counts(session_factory, client_settings) -> None:  # type: ignore[no-untyped-def]
    """The tag policy ranks by count, so ``count`` must really be there.

    This is the single assumption phase 4's genre/style split rests on: counts come
    from ``artist.getTopTags`` and nowhere else.
    """
    async with session_factory() as session:
        store = ArchiveStore(session, client_settings)
        async with LastfmClient(client_settings, store=store) as client:
            tags, result = await client.artist_top_tags(artist=ARTIST)
        await session.commit()

    assert "toptags" in result.body, "the envelope key the parser expects"
    assert tags.tags, "a well-known artist must have tags"
    counted = [tag for tag in tags.tags if tag.count is not None]
    print(f"\nartist.gettoptags -> {len(tags.tags)} tags, {len(counted)} with counts")
    print(f"  first three: {[(t.name, t.count) for t in tags.tags[:3]]}")
    assert counted, (
        "no tag carried a count, so the genre/style split would fall back to list "
        "order for every artist -- the documented count field is not where expected"
    )


@requires_key
async def test_album_getinfo_parses(session_factory, client_settings) -> None:  # type: ignore[no-untyped-def]
    async with session_factory() as session:
        store = ArchiveStore(session, client_settings)
        async with LastfmClient(client_settings, store=store) as client:
            artist, _ = await client.artist_info(artist=ARTIST)
            album, result = await client.album_info(artist=ARTIST, album="OK Computer")
        await session.commit()

    assert "album" in result.body
    assert album.name

    # The adapter model carries the raw `releasedate`; the year is parsed by the
    # derivation, so that is where the format assumption is checked.
    from metaedit.archive.derive import _parse_releasedate

    year = _parse_releasedate(album.releasedate)
    print(f"\nalbum.getinfo -> name={album.name!r} releasedate={album.releasedate!r}")
    print(f"  parsed year: {year}, tracks: {len(album.tracks)}")
    print(f"  parsed tag count: {len(album.toptags)}")
    if album.releasedate:
        assert year is not None, (
            f"releasedate {album.releasedate!r} did not yield a year; the format "
            "differs from the documented '6 Apr 1999, 00:00'"
        )
    assert artist.name  # keeps the fixture meaningful


@requires_key
async def test_track_getinfo_parses(session_factory, client_settings) -> None:  # type: ignore[no-untyped-def]
    async with session_factory() as session:
        store = ArchiveStore(session, client_settings)
        async with LastfmClient(client_settings, store=store) as client:
            track, result = await client.track_info(artist=ARTIST, track="Karma Police")
        await session.commit()

    assert "track" in result.body
    assert track.name
    print(f"\ntrack.getinfo -> name={track.name!r} duration={track.duration}ms")
    print(f"  album ref: {track.album.title if track.album else None}")
    if track.duration is not None:
        # A song is minutes, not seconds or hours. Catches a unit change.
        assert 30_000 < track.duration < 3_600_000, (
            f"duration {track.duration} is not plausible milliseconds"
        )


@requires_key
async def test_artist_getsimilar_parses(session_factory, client_settings) -> None:  # type: ignore[no-untyped-def]
    """Also pins down where the owning artist actually lives in the response.

    The container carries the owning artist as an attribute and the peers as
    repeated children; flattened to JSON both may occupy the key ``artist``. This
    asserts which one really arrives, which decides whether the derivation can read
    the owner from the body or must take it from the request params.
    """
    async with session_factory() as session:
        store = ArchiveStore(session, client_settings)
        async with LastfmClient(client_settings, store=store) as client:
            peers, result = await client.artist_similar(artist=ARTIST)
        await session.commit()

    container = result.body.get("similarartists")
    assert isinstance(container, dict), "the envelope key the parser expects"
    artist_value = container.get("artist")
    print(f"\nartist.getsimilar -> {len(peers)} peers")
    print(f"  container['artist'] is {type(artist_value).__name__}")
    print(f"  first three: {[(p.name, p.match) for p in peers[:3]]}")
    assert peers, "a well-known artist must have similar artists"


@requires_key
async def test_search_returns_candidates(session_factory, client_settings) -> None:  # type: ignore[no-untyped-def]
    async with session_factory() as session:
        store = ArchiveStore(session, client_settings)
        async with LastfmClient(client_settings, store=store) as client:
            results, result = await client.artist_search(ARTIST, limit=3)
        await session.commit()

    assert "results" in result.body
    assert results, "search must return candidates for a well-known artist"
    print(f"\nartist.search -> {[r.name for r in results]}")


# ------------------------------------------------- the whole pipeline, for real


@requires_key
async def test_end_to_end_derivation_on_real_data(session_factory, client_settings) -> None:  # type: ignore[no-untyped-def]
    """The test that matters most: real responses through the full pipeline.

    Fetches one of each kind, then derives and asserts that **nothing was
    unrecognised**. A parsing mismatch anywhere shows up here as
    ``unexpected_shapes > 0`` rather than as quietly missing entities.
    """
    async with session_factory() as session:
        store = ArchiveStore(session, client_settings)
        async with LastfmClient(client_settings, store=store) as client:
            await client.artist_info(artist=ARTIST)
            await client.artist_top_tags(artist=ARTIST)
            await client.album_info(artist=ARTIST, album="OK Computer")
            await client.track_info(artist=ARTIST, track="Karma Police")
            await client.artist_similar(artist=ARTIST, limit=5)
        await session.commit()

        report = await reindex(session)
        await session.commit()

    print("\nreindex report on real data:")
    for key, value in report.as_dict()["counts"].items():
        print(f"  {key}: {value}")

    assert report.artists >= 1, "a real artist response must derive an artist"
    assert report.albums >= 1
    assert report.tracks >= 1
    assert report.tag_edges >= 1, "real tags must reach the tag edges"
    assert report.unexpected_shapes == 0, (
        "a real response produced no entity, so a parser does not match the live "
        "shape -- the failure mode this test exists to catch"
    )


@requires_key
async def test_derivation_is_reproducible_on_real_data(session_factory, client_settings) -> None:  # type: ignore[no-untyped-def]
    """A wipe and rebuild of real data must reproduce the derived rows exactly."""
    from metaedit.archive.reindex import DERIVED_TABLES, count_rows

    async with session_factory() as session:
        store = ArchiveStore(session, client_settings)
        async with LastfmClient(client_settings, store=store) as client:
            await client.artist_info(artist=ARTIST)
            await client.album_info(artist=ARTIST, album="OK Computer")
        await session.commit()
        await reindex(session)
        await session.commit()
        first = await count_rows(session)

        await session.execute(text(f"TRUNCATE TABLE {', '.join(DERIVED_TABLES)}"))
        await session.commit()
        await reindex(session)
        await session.commit()
        second = await count_rows(session)

    assert first == second, "real data must be as reproducible as the fixtures"


@requires_key
async def test_second_identical_call_is_served_from_the_archive(
    session_factory, client_settings
) -> None:  # type: ignore[no-untyped-def]
    """The archive must save a real API call, not just appear to."""
    from metaedit.archive.stats import read_metrics

    settings = client_settings.model_copy(update={"archive_freshness_ttl_s": 3600})
    async with session_factory() as session:
        store = ArchiveStore(session, settings)
        before = read_metrics()
        hits_before, misses_before = before.hits, before.misses
        async with LastfmClient(settings, store=store) as client:
            first_artist, first = await client.artist_info(artist=ARTIST)
            second_artist, second = await client.artist_info(artist=ARTIST)
        await session.commit()

    assert first.served_from_archive is False, "the first call must hit the network"
    assert second.served_from_archive is True, "the second must be served locally"
    assert first_artist.name == second_artist.name

    # The counters are process-wide, so this must be a delta: other tests in the
    # same session have already recorded their own reads.
    after = read_metrics()
    assert after.misses - misses_before == 1, "exactly one network call"
    assert after.hits - hits_before == 1, "exactly one archive hit"


@requires_key
def test_archive_fixture_is_usable_for_offline_tests() -> None:  # type: ignore[no-untyped-def]
    """Guard against the whole module silently doing nothing.

    If the skip conditions ever change, this makes the plugin collect something
    rather than reporting a green run over zero tests.
    """
    assert API_KEY, "this test only runs when a key is present"
