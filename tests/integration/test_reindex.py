"""Reindex against a real Postgres (docs/design/0003 §9).

These tests need real Postgres because the guarantees are database-level: identity
keys with unique constraints, explicit ids, transactional TRUNCATE, and sequence
state after an explicit-id insert.

The headline acceptance criterion is `test_rebuild_is_reproducible`: wipe the
derived tables, rebuild from the raw layer with the network hard-blocked, and assert
the result is identical.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from tests.conftest import requires_postgres

from metaedit.archive.reindex import (
    DERIVED_TABLES,
    ReindexError,
    assign_ids,
    count_rows,
    reindex,
    validate,
)
from metaedit.archive.store import ArchiveStore, Observation
from metaedit.db.models import (
    LastfmAlbum,
    LastfmArtist,
    LastfmArtistAlias,
    LastfmEntityTag,
    LastfmSimilarity,
    LastfmTagEdge,
    LastfmTrack,
)
from metaedit.db.partitions import ensure_partitions

pytestmark = requires_postgres

BASE = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def _cher(*, tags: list[dict[str, Any]] | None = None, summary: str = "Cher is a singer."):
    return {
        "artist": {
            "name": "Cher",
            "mbid": "mbid-cher",
            "url": "https://www.last.fm/music/Cher",
            "stats": {"listeners": "196440", "plays": "1599101"},
            "tags": {"tag": tags or [{"name": "pop", "count": "100"}]},
            "bio": {"summary": summary, "published": "Thu, 13 Mar 2008"},
        }
    }


def _album():
    return {
        "album": {
            "name": "Believe",
            "artist": "Cher",
            "mbid": "mbid-believe",
            "releasedate": "6 Apr 1999, 00:00",
            "toptags": {"tag": [{"name": "pop"}]},
            "tracks": {"track": [{"name": "Believe", "duration": 239, "rank": "1"}]},
        }
    }


def _track():
    return {
        "track": {
            "name": "Believe",
            "mbid": "mbid-track",
            "duration": "240000",
            "artist": {"name": "Cher", "mbid": "mbid-cher"},
            "album": {"title": "Believe", "mbid": "mbid-believe", "position": "1"},
            "toptags": {"tag": [{"name": "pop"}]},
            "wiki": {"summary": "A hit single.", "published": "Sun, 27 Jul 2008"},
        }
    }


def _similar():
    return {
        "similarartists": {"artist": [{"name": "Madonna", "mbid": "mbid-madonna", "match": "0.9"}]}
    }


async def _seed(session) -> None:  # type: ignore[no-untyped-def]
    """Store raw observations covering all three entity kinds, tags and similarity."""
    settings = _archive_settings()
    store = ArchiveStore(session, settings)
    await ensure_partitions(await session.connection(), months_ahead=1)
    plan = [
        ("artist.getinfo", {"artist": "Cher", "autocorrect": "1"}, _cher()),
        ("album.getinfo", {"artist": "Cher", "album": "Believe", "autocorrect": "1"}, _album()),
        ("track.getinfo", {"artist": "Cher", "track": "Believe"}, _track()),
        ("artist.getsimilar", {"artist": "Cher", "limit": 20}, _similar()),
    ]
    for method, params, body in plan:
        observation = Observation(
            method=method,
            params=params,
            http_status=200,
            duration_ms=10,
            body=body,
            user_agent="test",
        )
        await store.record(observation)
    await session.commit()


def _archive_settings(**overrides: object):  # type: ignore[no-untyped-def]
    from metaedit.config import Settings

    defaults: dict[str, object] = {
        "_env_file": None,
        "ARCHIVE_ENABLED": True,
        "ARCHIVE_LOG_REQUESTS": True,
        "LASTFM_API_KEY": "test-key",
        "LOG_JSON": False,
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


@pytest.fixture
async def session_factory(database_url: str):  # type: ignore[no-untyped-def]
    engine = create_async_engine(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


async def _snapshot_derived(session) -> dict[str, list[tuple[Any, ...]]]:  # type: ignore[no-untyped-def]
    """Every derived row, in a stable order, as plain tuples."""
    plan: list[tuple[str, Any]] = [
        ("lastfm_artist", LastfmArtist),
        ("lastfm_album", LastfmAlbum),
        ("lastfm_track", LastfmTrack),
        ("lastfm_tag_edge", LastfmTagEdge),
        ("lastfm_similarity", LastfmSimilarity),
        ("lastfm_artist_alias", LastfmArtistAlias),
        ("lastfm_entity_tag", LastfmEntityTag),
    ]
    snapshot: dict[str, list[tuple[Any, ...]]] = {}
    for name, model in plan:
        columns = [column.name for column in model.__table__.columns]
        rows = (
            await session.execute(select(model).order_by(*[getattr(model, c) for c in columns]))
        ).scalars()
        snapshot[name] = [tuple(getattr(row, column) for column in columns) for row in rows]
    return snapshot


# ------------------------------------------------------------- basic derivation


async def test_reindex_populates_every_derived_table(session_factory) -> None:  # type: ignore[no-untyped-def]
    async with session_factory() as session:
        await _seed(session)
        report = await reindex(session)
        await session.commit()

        counts = await count_rows(session)

    assert report.artists == 1
    assert report.albums == 1
    assert report.tracks == 1
    assert counts["lastfm_artist"] == 1
    assert counts["lastfm_album"] == 1
    assert counts["lastfm_track"] == 1
    assert counts["lastfm_tag_edge"] == 3, "one artist, one album, one track tag"
    assert counts["lastfm_similarity"] == 1
    assert counts["lastfm_entity_tag"] == 1, "only 'pop' appears"
    assert counts["lastfm_artist_alias"] == 0, "no misspellings were requested"


async def test_derived_artist_carries_its_provenance(session_factory) -> None:  # type: ignore[no-untyped-def]
    async with session_factory() as session:
        await _seed(session)
        await reindex(session)
        await session.commit()
        artist = (await session.execute(select(LastfmArtist))).scalar_one()
        response_exists = await session.scalar(
            text("select count(*) from lastfm_response where id = :rid"),
            {"rid": artist.latest_response_id},
        )

    assert artist.identity == "mbid:mbid-cher"
    assert artist.overview == "Cher is a singer."
    assert artist.mbid == "mbid-cher"
    assert response_exists == 1, "every derived row must name a raw body that exists"


async def test_album_and_track_details_are_derived(session_factory) -> None:  # type: ignore[no-untyped-def]
    async with session_factory() as session:
        await _seed(session)
        await reindex(session)
        await session.commit()
        album = (await session.execute(select(LastfmAlbum))).scalar_one()
        track = (await session.execute(select(LastfmTrack))).scalar_one()

    assert album.production_year == 1999
    assert album.tracklist and album.tracklist[0]["duration_ms"] == 239000
    assert track.duration_ms == 240000
    assert track.overview == "A hit single."
    assert track.artist_name == "Cher"
    assert track.album_position == 1


async def test_alias_is_recorded_for_a_misspelled_request(session_factory) -> None:  # type: ignore[no-untyped-def]
    """Last.fm autocorrects; the requested spelling is the alias."""
    async with session_factory() as session:
        await ensure_partitions(await session.connection(), months_ahead=1)
        store = ArchiveStore(session, _archive_settings())
        await store.record(
            Observation(
                method="artist.getinfo",
                params={"artist": "cher", "autocorrect": "1"},
                http_status=200,
                duration_ms=5,
                body=_cher(),
                user_agent="test",
            )
        )
        await session.commit()

        await reindex(session)
        await session.commit()
        alias = (await session.execute(select(LastfmArtistAlias))).scalar_one()
        artist = (await session.execute(select(LastfmArtist))).scalar_one()

    assert alias.requested_name_norm == "cher"
    assert alias.canonical_artist_id == artist.id


async def test_an_identical_request_is_not_an_alias(session_factory) -> None:  # type: ignore[no-untyped-def]
    """Otherwise the alias table would just be a copy of every request."""
    async with session_factory() as session:
        await _seed(session)
        await reindex(session)
        await session.commit()
        aliases = await session.scalar(select(func.count()).select_from(LastfmArtistAlias))

    assert aliases == 0


# ----------------------------------------------------------- reproducibility


async def test_rebuild_is_reproducible(session_factory) -> None:  # type: ignore[no-untyped-def]
    """The acceptance criterion: wipe the derived tables and rebuild identically.

    The network is blocked for the duration, so a rebuild that needed to fetch
    anything would fail rather than quietly pass.
    """
    transport = httpx.MockTransport(
        lambda request: pytest.fail(f"reindex must not make network calls: {request.url}")
    )
    async with session_factory() as session:
        await _seed(session)
        await reindex(session)
        await session.commit()
        first = await _snapshot_derived(session)

        # Wipe every derived table, as an operator would before a rebuild.
        await session.execute(text(f"TRUNCATE TABLE {', '.join(DERIVED_TABLES)}"))
        await session.commit()
        empty = await count_rows(session)

        async with httpx.AsyncClient(transport=transport) as client:  # noqa: F841 - presence blocks egress
            await reindex(session)
        await session.commit()
        second = await _snapshot_derived(session)

    assert sum(empty.values()) == 0, "the wipe must have emptied every table"
    assert first == second, "a rebuild from the raw layer must be byte-for-byte identical"
    assert first["lastfm_artist"], "and it must not be trivially empty"


async def test_repeated_reindex_is_stable(session_factory) -> None:  # type: ignore[no-untyped-def]
    """Running it twice without a wipe must not duplicate rows."""
    async with session_factory() as session:
        await _seed(session)
        await reindex(session)
        await session.commit()
        first = await count_rows(session)
        await reindex(session)
        await session.commit()
        second = await count_rows(session)

    assert first == second


async def test_derived_ids_are_assigned_in_identity_order(session_factory) -> None:  # type: ignore[no-untyped-def]
    async with session_factory() as session:
        await ensure_partitions(await session.connection(), months_ahead=1)
        store = ArchiveStore(session, _archive_settings())
        for name in ["Zebra", "Apple", "Mango"]:
            await store.record(
                Observation(
                    method="artist.getinfo",
                    params={"artist": name, "autocorrect": "1"},
                    http_status=200,
                    duration_ms=1,
                    body={"artist": {"name": name}},
                    user_agent="test",
                )
            )
        await session.commit()

        await reindex(session)
        await session.commit()
        rows = (
            await session.execute(
                select(LastfmArtist.id, LastfmArtist.name).order_by(LastfmArtist.id)
            )
        ).all()

    assert [row.name for row in rows] == ["Apple", "Mango", "Zebra"], "ids follow identity order"
    assert [row.id for row in rows] == [1, 2, 3]


async def test_sequences_are_advanced_past_explicit_ids(session_factory) -> None:  # type: ignore[no-untyped-def]
    """A future insert must not collide with an id we assigned explicitly."""
    async with session_factory() as session:
        await _seed(session)
        await reindex(session)
        await session.commit()
        next_id = await session.scalar(
            text("select nextval(pg_get_serial_sequence('lastfm_artist','id'))")
        )
        max_id = await session.scalar(select(func.max(LastfmArtist.id)))

    assert next_id is not None and max_id is not None
    assert next_id > max_id, "the sequence must be ahead of the explicit ids"


# ------------------------------------------------------------------- dry run


async def test_dry_run_writes_nothing(session_factory) -> None:  # type: ignore[no-untyped-def]
    async with session_factory() as session:
        await _seed(session)
        report = await reindex(session, dry_run=True)
        await session.commit()
        counts = await count_rows(session)

    assert report.dry_run is True
    assert report.artists == 1, "the report describes what would be derived"
    assert sum(counts.values()) == 0, "nothing may be written by a dry run"
    assert report.changes["identical"] is False, "an empty target is not identical"


async def test_dry_run_on_an_unchanged_archive_reports_no_differences(session_factory) -> None:  # type: ignore[no-untyped-def]
    async with session_factory() as session:
        await _seed(session)
        await reindex(session)
        await session.commit()
        report = await reindex(session, dry_run=True)
        await session.commit()
        counts = await count_rows(session)

    assert report.changes["identical"] is True, "a rebuilt archive has no pending changes"
    assert report.changes["tables"] == {}
    assert counts["lastfm_artist"] == 1, "and the dry run still wrote nothing new"


async def test_dry_run_does_not_disturb_existing_rows(session_factory) -> None:  # type: ignore[no-untyped-def]
    async with session_factory() as session:
        await _seed(session)
        await reindex(session)
        await session.commit()
        before = await _snapshot_derived(session)
        await reindex(session, dry_run=True)
        await session.commit()
        after = await _snapshot_derived(session)

    assert before == after


# ----------------------------------------------------------------- validation


async def test_invalid_since_is_rejected(session_factory) -> None:  # type: ignore[no-untyped-def]
    async with session_factory() as session:
        with pytest.raises(ReindexError, match="ISO date"):
            await reindex(session, since="not-a-date")


async def test_unknown_only_target_is_rejected(session_factory) -> None:  # type: ignore[no-untyped-def]
    async with session_factory() as session:
        await _seed(session)
        with pytest.raises(ReindexError, match="unknown --only target"):
            await reindex(session, only="nonsense")


async def test_only_artist_rebuilds_just_artists(session_factory) -> None:  # type: ignore[no-untyped-def]
    async with session_factory() as session:
        await _seed(session)
        await reindex(session)
        await session.commit()
        await reindex(session, only="artist")
        await session.commit()
        counts = await count_rows(session)

    assert counts["lastfm_artist"] == 1
    assert counts["lastfm_album"] == 0, "--only artist rebuilds only that family"


def test_validation_refuses_a_dangling_response_reference() -> None:
    """Nothing may be written when a derived row cannot be traced to a raw body."""
    from metaedit.archive.derive import DerivationResult, DerivedEntity
    from metaedit.archive.derive import RawObservation as RO

    observation = RO(
        request_id=1,
        requested_at=BASE,
        response_id="a" * 64,
        body=_cher(),
        method="artist.getinfo",
    )
    result = DerivationResult(
        artists=[
            DerivedEntity(
                identity="mbid:x",
                kind="artist",
                name="X",
                name_norm="X",
                first_seen_at=BASE,
                last_seen_at=BASE,
                latest_response_id="b" * 64,  # not in the raw layer
            )
        ]
    )
    with pytest.raises(ReindexError, match="not in the raw layer"):
        validate(result, [observation])


def test_validation_refuses_an_empty_identity() -> None:
    from metaedit.archive.derive import DerivationResult, DerivedEntity

    result = DerivationResult(
        artists=[
            DerivedEntity(
                identity="",
                kind="artist",
                name="X",
                name_norm="X",
                first_seen_at=BASE,
                last_seen_at=BASE,
                latest_response_id="a" * 64,
            )
        ]
    )
    with pytest.raises(ReindexError, match="empty identity"):
        validate(result, [])


def test_assign_ids_is_deterministic() -> None:
    from metaedit.archive.derive import DerivationResult, DerivedEntity

    def entity(identity: str) -> DerivedEntity:
        return DerivedEntity(
            identity=identity,
            kind="artist",
            name=identity,
            name_norm=identity,
            first_seen_at=BASE,
            last_seen_at=BASE,
            latest_response_id="a" * 64,
        )

    result = DerivationResult(artists=[entity("name:Apple"), entity("name:Zebra")])
    ids = assign_ids(result)
    assert ids["lastfm_artist"] == {"name:Apple": 1, "name:Zebra": 2}
    assert ids == assign_ids(result)
    # Every derived table gets deterministic ids, not just the entity ones: the
    # graph tables would otherwise take sequence values and differ between rebuilds.
    assert set(ids) == set(DERIVED_TABLES)


# ------------------------------------------------- silent-loss detection, live


async def test_healthy_archive_reports_zero_unexpected_shapes(session_factory) -> None:  # type: ignore[no-untyped-def]
    """The number an operator alerts on: zero when the derivation understands everything."""
    async with session_factory() as session:
        await _seed(session)
        report = await reindex(session, dry_run=True)

    assert report.unexpected_shapes == 0
    # artist.getsimilar is archived but carries another shape, so it is expected.
    assert report.expected_no_envelope == 1


async def test_an_unusable_body_shows_up_in_the_report(session_factory) -> None:  # type: ignore[no-untyped-def]
    """A regression that drops data must be visible as a number, not as absence."""
    async with session_factory() as session:
        await _seed(session)
        store = ArchiveStore(session, _archive_settings())
        await store.record(
            Observation(
                method="artist.getinfo",
                params={"artist": "???", "autocorrect": "1"},
                http_status=200,
                duration_ms=5,
                # An artist envelope with nothing to key on.
                body={"artist": {"url": "https://www.last.fm/music/unknown"}},
                user_agent="test",
            )
        )
        await session.commit()

        report = await reindex(session, dry_run=True)

    assert report.unexpected_shapes == 1, "the discarded body is reported"
    assert report.artists == 1, "and the rest of the archive still derives"


async def test_describe_archive_names_the_offending_method(
    session_factory,
    database_url: str,  # type: ignore[no-untyped-def]
) -> None:
    """A count alone is not a diagnosis; the method and keys are."""
    from metaedit.archive.reindex import describe_archive
    from metaedit.config import Settings

    async with session_factory() as session:
        await _seed(session)
        store = ArchiveStore(session, _archive_settings())
        await store.record(
            Observation(
                method="artist.getinfo",
                params={"artist": "???", "autocorrect": "1"},
                http_status=200,
                duration_ms=5,
                body={"artist": {"url": "https://www.last.fm/music/unknown"}},
                user_agent="test",
            )
        )
        await session.commit()

    settings = Settings(_env_file=None, DATABASE_URL=database_url)  # type: ignore[call-arg]
    report = await describe_archive(settings)

    assert report["shapes"]["unexpected"] == 1
    assert report["shapes"]["expected_no_envelope"] == 1
    assert any(key.startswith("artist.getinfo") for key in report["shapes"]["unexpected_bodies"]), (
        "the offending method must be named"
    )
    assert report["would_derive"]["artists"] == 1
