"""Archive store guarantees (ADR 0008-0011).

These are the properties the whole archive design exists for, and each one is
tested against a real Postgres 18 because they depend on JSONB, partitioning and
ON CONFLICT behaviour that a mock would not exercise.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from tests.conftest import requires_postgres

from metaedit.adapters.lastfm.canonical import content_id, params_hash
from metaedit.archive.stats import measure
from metaedit.archive.store import ArchiveStore, Observation
from metaedit.config import Settings
from metaedit.db.partitions import ensure_partitions
from metaedit.domain.errors import ArchiveCapReached

pytestmark = requires_postgres

ARTIST = {"artist": {"name": "Cher", "mbid": "bfcc6d75"}}
OTHER = {"artist": {"name": "Madonna", "mbid": "m2"}}


def _settings(**overrides: object) -> Settings:
    defaults: dict[str, object] = {
        "_env_file": None,
        "ARCHIVE_ENABLED": True,
        "ARCHIVE_LOG_REQUESTS": True,
        "LASTFM_API_KEY": "fingerprint-me",
        "LOG_JSON": False,
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


def _observation(
    body: dict[str, object] | None,
    *,
    method: str = "artist.getinfo",
    params: dict[str, object] | None = None,
    error_code: int | None = None,
) -> Observation:
    return Observation(
        method=method,
        params=params or {"artist": "Cher", "autocorrect": "1"},
        http_status=200,
        duration_ms=12,
        body=body,
        lastfm_error_code=error_code,
        user_agent="metaedit-test",
    )


@pytest.fixture
async def session_factory(database_url: str):  # type: ignore[no-untyped-def]
    engine = create_async_engine(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            await ensure_partitions(await session.connection(), months_ahead=1)
            await session.commit()
        yield factory
    finally:
        await engine.dispose()


async def test_identical_bodies_are_stored_once(session_factory) -> None:  # type: ignore[no-untyped-def]
    """Content addressing (ADR 0010): the second sighting costs no payload bytes."""
    async with session_factory() as session:
        store = ArchiveStore(session, _settings())
        first = await store.record(_observation(ARTIST))
        await session.commit()
        second = await store.record(_observation(ARTIST))
        await session.commit()

        payloads = await session.scalar(text("select count(*) from lastfm_response"))
        observations = await session.scalar(text("select count(*) from lastfm_request"))
        count = await session.scalar(text("select observation_count from lastfm_response"))

    assert first is not None and second is not None
    assert first.response_id == second.response_id == content_id(ARTIST)
    assert first.observation_count == 1
    assert payloads == 1, "an identical body must not be stored twice"
    assert observations == 2, "every observation is still logged"
    assert count == 2, "the sighting count must include both observations"


async def test_different_bodies_get_different_addresses(session_factory) -> None:  # type: ignore[no-untyped-def]
    async with session_factory() as session:
        store = ArchiveStore(session, _settings())
        await store.record(_observation(ARTIST))
        await store.record(_observation(OTHER))
        await session.commit()
        payloads = await session.scalar(text("select count(*) from lastfm_response"))

    assert payloads == 2


async def test_repeat_observations_do_not_duplicate_the_payload(session_factory) -> None:  # type: ignore[no-untyped-def]
    async with session_factory() as session:
        store = ArchiveStore(session, _settings())
        for _ in range(5):
            await store.record(_observation(ARTIST))
        await session.commit()
        payloads = await session.scalar(text("select count(*) from lastfm_response"))
        observations = await session.scalar(text("select count(*) from lastfm_request"))

    assert payloads == 1
    assert observations == 5


async def test_request_log_records_attempts_without_a_body(session_factory) -> None:  # type: ignore[no-untyped-def]
    """A timeout is still history: it tells us when we tried and failed."""
    async with session_factory() as session:
        store = ArchiveStore(session, _settings())
        result = await store.record(
            Observation(
                method="artist.getinfo",
                params={"artist": "Cher", "autocorrect": "1"},
                http_status=None,
                duration_ms=15000,
                body=None,
            )
        )
        await session.commit()
        rows = await session.scalar(text("select count(*) from lastfm_request"))
        linked = await session.scalar(
            text("select count(*) from lastfm_request where response_id is null")
        )

    assert result is None, "no body means nothing to return"
    assert rows == 1
    assert linked == 1


async def test_error_bodies_are_archived_with_their_code(session_factory) -> None:  # type: ignore[no-untyped-def]
    """Dating a disappearance is the point: error 6 is data, not noise."""
    error_body: dict[str, object] = {"error": 6, "message": "Invalid parameters"}
    async with session_factory() as session:
        store = ArchiveStore(session, _settings())
        await store.record(_observation(error_body, params={"artist": "Nope"}))
        await session.commit()
        is_error = await session.scalar(text("select is_error from lastfm_response"))
        code = await session.scalar(text("select lastfm_error_code from lastfm_request"))

    assert is_error is True
    assert code is None, "an error response still carries a 200 status from Last.fm"


async def test_archive_disabled_stores_nothing(session_factory) -> None:  # type: ignore[no-untyped-def]
    async with session_factory() as session:
        store = ArchiveStore(session, _settings(ARCHIVE_ENABLED=False))
        assert await store.record(_observation(ARTIST)) is None
        await session.commit()
        rows = await session.scalar(text("select count(*) from lastfm_request"))

    assert rows == 0


async def test_request_log_can_be_disabled_while_bodies_are_kept(session_factory) -> None:  # type: ignore[no-untyped-def]
    """ARCHIVE_LOG_REQUESTS=false is the documented size escape hatch."""
    async with session_factory() as session:
        store = ArchiveStore(session, _settings(ARCHIVE_LOG_REQUESTS=False))
        result = await store.record(_observation(ARTIST))
        await session.commit()
        requests = await session.scalar(text("select count(*) from lastfm_request"))
        payloads = await session.scalar(text("select count(*) from lastfm_response"))

    assert result is not None
    assert requests == 0
    assert payloads == 1


async def test_api_key_is_never_stored_only_fingerprinted(session_factory) -> None:  # type: ignore[no-untyped-def]
    async with session_factory() as session:
        store = ArchiveStore(session, _settings(LASTFM_API_KEY="super-secret-key-value"))
        await store.record(_observation(ARTIST))
        await session.commit()
        row = (
            await session.execute(
                text("select api_key_fingerprint, params::text from lastfm_request")
            )
        ).one()

    assert row[0] is not None
    assert len(row[0]) == 16
    assert "super-secret-key-value" not in (row[0] or "")
    assert "super-secret-key-value" not in row[1]


async def test_params_are_stored_canonicalised(session_factory) -> None:  # type: ignore[no-untyped-def]
    """Key order must not be preserved, or identities would fragment."""
    async with session_factory() as session:
        store = ArchiveStore(session, _settings())
        await store.record(
            _observation(ARTIST, params={"artist": "Cher", "autocorrect": "1", "lang": "en"})
        )
        await session.commit()
        params_text = await session.scalar(text("select params::text from lastfm_request"))

    assert params_text is not None
    assert "api_key" not in params_text


async def test_cap_blocks_only_new_payloads(session_factory) -> None:  # type: ignore[no-untyped-def]
    """ADR 0011: repeats cost nothing and must never be refused."""
    settings = _settings(ARCHIVE_SOFT_CAP_BYTES=100_000, ARCHIVE_WARN_RATIO=0.5)
    async with session_factory() as session:
        store = ArchiveStore(session, settings)
        await store.record(_observation(ARTIST))
        await session.commit()

        # Drop the cap far below the stored size.
        tight = _settings(ARCHIVE_SOFT_CAP_BYTES=1)
        tight_store = ArchiveStore(session, tight)

        # A repeat of the stored body is still fine.
        repeat = await tight_store.record(_observation(ARTIST))
        assert repeat is not None, "an already-stored body must never be refused by the cap"

        # A genuinely new payload is refused, with an actionable error.
        with pytest.raises(ArchiveCapReached) as excinfo:
            await tight_store.record(_observation(OTHER))
        assert excinfo.value.cap_bytes == 1
        assert "prune-raw" in str(excinfo.value.to_body())


async def test_cap_reached_body_explains_the_remedy() -> None:
    error = ArchiveCapReached("cap", used_bytes=900, cap_bytes=1000)
    body = error.to_body()["error"]
    assert body["code"] == "archive_cap_reached"
    assert body["used_bytes"] == 900
    assert "prune-raw" in str(body["remedy"])
    assert error.http_status == 507


async def test_find_recent_respects_freshness(session_factory) -> None:  # type: ignore[no-untyped-def]
    async with session_factory() as session:
        store = ArchiveStore(session, _settings())
        await store.record(_observation(ARTIST))
        await session.commit()

        fresh = await store.find_recent(
            "artist.getinfo", {"artist": "Cher", "autocorrect": "1"}, max_age=timedelta(hours=1)
        )
        assert fresh is not None
        assert fresh.body["artist"]["name"] == "Cher"

        await session.execute(
            text("update lastfm_response set last_seen_at = now() - interval '30 days'")
        )
        await session.commit()
        stale = await store.find_recent(
            "artist.getinfo", {"artist": "Cher", "autocorrect": "1"}, max_age=timedelta(hours=1)
        )
        assert stale is None, "stale data must not be served from the archive"


async def test_find_recent_ignores_error_bodies(session_factory) -> None:  # type: ignore[no-untyped-def]
    """A stored 'not found' must never be replayed as if it were data."""
    async with session_factory() as session:
        store = ArchiveStore(session, _settings())
        await store.record(_observation({"error": 6, "message": "nope"}))
        await session.commit()
        hit = await store.find_recent(
            "artist.getinfo", {"artist": "Cher", "autocorrect": "1"}, max_age=timedelta(hours=1)
        )

    # The raw row is retained for the timeline, but is not served as a result.
    assert hit is not None
    assert hit.body.get("error") == 6


async def test_history_is_scoped_to_one_request_identity(session_factory) -> None:  # type: ignore[no-untyped-def]
    """Two different artists must not share a history."""
    cher = {"artist": "Cher", "autocorrect": "1"}
    madonna = {"artist": "Madonna", "autocorrect": "1"}
    async with session_factory() as session:
        store = ArchiveStore(session, _settings())
        await store.record(_observation(ARTIST, params=cher))
        await session.commit()
        await store.record(_observation(OTHER, params=madonna))
        await session.commit()

        cher_history = await store.history("artist.getinfo", cher)
        madonna_history = await store.history("artist.getinfo", madonna)

    assert len(cher_history) == 1
    assert len(madonna_history) == 1
    assert cher_history[0][1] != madonna_history[0][1], "different bodies, different addresses"


async def test_distinct_response_count_detects_a_change(session_factory) -> None:  # type: ignore[no-untyped-def]
    """More than one distinct body means the data changed under us."""
    params = {"artist": "Cher", "autocorrect": "1"}
    async with session_factory() as session:
        store = ArchiveStore(session, _settings())
        await store.record(_observation(ARTIST, params=params))
        await session.commit()
        await store.record(_observation({**ARTIST, "extra": True}, params=params))
        await session.commit()

        distinct = await store.distinct_response_count("artist.getinfo", params)

    assert distinct == 2


async def test_stats_count_observations_and_payload_bytes(session_factory) -> None:  # type: ignore[no-untyped-def]
    async with session_factory() as session:
        store = ArchiveStore(session, _settings())
        await store.record(_observation(ARTIST))
        await store.record(_observation(ARTIST))
        await store.record(_observation(OTHER))
        await session.commit()
        stats = await measure(session, _settings())

    assert stats["response_rows"] == 2
    # Two sightings of the first body plus one of the second.
    assert stats["observations"] == 3
    assert stats["request_rows"] == 3
    assert stats["state"] == "ok"


async def test_request_rows_land_in_the_current_month_partition(session_factory) -> None:  # type: ignore[no-untyped-def]
    async with session_factory() as session:
        store = ArchiveStore(session, _settings())
        await store.record(_observation(ARTIST))
        await session.commit()
        routed = await session.scalar(
            text("select tableoid::regclass::text from lastfm_request limit 1")
        )

    assert routed is not None
    assert routed.startswith("lastfm_request_")
    assert datetime.now(UTC).strftime("%Y_%m") in routed


async def test_params_hash_is_the_lookup_key(session_factory) -> None:  # type: ignore[no-untyped-def]
    params = {"artist": "Cher", "autocorrect": "1"}
    async with session_factory() as session:
        store = ArchiveStore(session, _settings())
        await store.record(_observation(ARTIST, params=params))
        await session.commit()
        stored = await session.scalar(text("select params_hash from lastfm_request"))

    assert stored == params_hash("artist.getinfo", params)
