"""Archive byte accounting and the cap policy.

The cap behaviour is a Terms-of-Service obligation, so it is tested rather than
assumed: a new distinct payload must be refused at the cap, while a repeat
observation of an already-stored body is always allowed.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from tests.conftest import requires_postgres

from metaedit.archive.stats import can_store_new_payload, measure, partition_usage
from metaedit.config import Settings

pytestmark = requires_postgres


def _settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg,arg-type]


@pytest.fixture
async def session_factory(database_url: str):  # type: ignore[no-untyped-def]
    """A migrated database with its monthly partitions pre-created.

    The migration deliberately creates no partitions: the app (and this fixture)
    ensure them, so a fresh database started in any month works.
    """
    from metaedit.db.partitions import ensure_partitions

    engine = create_async_engine(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            await ensure_partitions(await session.connection(), months_ahead=1)
            await session.commit()
        yield factory
    finally:
        await engine.dispose()


async def _store_response(session, *, key: str, payload: str) -> None:  # type: ignore[no-untyped-def]
    await session.execute(
        text(
            "insert into lastfm_response (id, body, body_bytes, observation_count) "
            "values (:id, cast(:body as jsonb), :n, 1) "
            "on conflict (id) do update set observation_count = "
            "  lastfm_response.observation_count + 1"
        ),
        {"id": key, "body": payload, "n": len(payload)},
    )


async def test_empty_archive_reports_ok(session_factory) -> None:  # type: ignore[no-untyped-def]
    async with session_factory() as session:
        stats = await measure(session, _settings())
    assert stats["payload_bytes"] == 0
    assert stats["state"] == "ok"
    assert stats["used_ratio"] == 0.0
    assert stats["headroom_bytes"] == stats["cap_bytes"]


async def test_measure_counts_bytes_rows_and_observations(session_factory) -> None:  # type: ignore[no-untyped-def]
    async with session_factory() as session:
        await _store_response(session, key="a" * 64, payload='{"artist": {"name": "Cher"}}')
        await _store_response(session, key="a" * 64, payload='{"artist": {"name": "Cher"}}')
        await _store_response(session, key="b" * 64, payload='{"artist": {"name": "Madonna"}}')
        await session.execute(
            text(
                "insert into lastfm_request (method, params, params_hash, requested_at, http_status) "
                "values ('artist.getinfo', '{}'::jsonb, :h, now(), 200)"
            ),
            {"h": "c" * 64},
        )
        await session.commit()
        stats = await measure(session, _settings())

    # Two distinct bodies, three observations total.
    assert stats["response_rows"] == 2
    assert stats["observations"] == 3
    assert stats["request_rows"] == 1
    assert stats["payload_bytes"] == sum(
        len(p) for p in ('{"artist": {"name": "Cher"}}', '{"artist": {"name": "Madonna"}}')
    )
    assert stats["oldest_request_at"] is not None


@pytest.mark.parametrize(
    ("stored", "expected_state"),
    [(0, "ok"), (80, "warning"), (100, "cap_reached"), (150, "cap_reached")],
)
async def test_state_thresholds(session_factory, stored: int, expected_state: str) -> None:  # type: ignore[no-untyped-def]
    # 100-byte cap: state is driven purely by stored payload bytes.
    async with session_factory() as session:
        if stored:
            await session.execute(
                text(
                    "insert into lastfm_response (id, body, body_bytes) "
                    "values (:id, '{}'::jsonb, :n)"
                ),
                {"id": "d" * 64, "n": stored},
            )
            await session.commit()
        stats = await measure(
            session, _settings(ARCHIVE_SOFT_CAP_BYTES=100, ARCHIVE_WARN_RATIO=0.8)
        )
    assert stats["state"] == expected_state


async def test_new_payloads_are_refused_at_the_cap(session_factory) -> None:  # type: ignore[no-untyped-def]
    settings = _settings(ARCHIVE_SOFT_CAP_BYTES=100)
    async with session_factory() as session:
        assert await can_store_new_payload(session, settings, incoming_bytes=10)
        await session.execute(
            text(
                "insert into lastfm_response (id, body, body_bytes) values (:id, '{}'::jsonb, 100)"
            ),
            {"id": "e" * 64},
        )
        await session.commit()
        assert not await can_store_new_payload(session, settings, incoming_bytes=1)


async def test_repeat_observations_are_never_blocked_by_the_cap(session_factory) -> None:  # type: ignore[no-untyped-def]
    """A repeat costs no new payload bytes, so the cap must not stop it.

    The first body alone already exceeds the (deliberately tiny) cap, so the
    second store is only possible if repeats genuinely bypass the cap check.
    """
    tiny_payload = '{"a": "0123456789abcdef"}'
    settings = _settings(ARCHIVE_SOFT_CAP_BYTES=10)
    assert len(tiny_payload) > settings.archive_soft_cap_bytes

    async with session_factory() as session:
        await _store_response(session, key="f" * 64, payload=tiny_payload)
        await _store_response(session, key="f" * 64, payload=tiny_payload)
        await session.commit()
        stats = await measure(session, settings)

    assert stats["state"] == "cap_reached"
    assert stats["response_rows"] == 1
    assert stats["observations"] == 2
    assert stats["payload_bytes"] == len(tiny_payload)


async def test_archive_disabled_never_stores(session_factory) -> None:  # type: ignore[no-untyped-def]
    async with session_factory() as session:
        assert not await can_store_new_payload(session, _settings(ARCHIVE_ENABLED=False), 1)


async def test_partition_usage_lists_monthly_partitions(session_factory) -> None:  # type: ignore[no-untyped-def]
    from metaedit.db.partitions import ensure_partitions

    async with session_factory() as session:
        await ensure_partitions(await session.connection(), months_ahead=1)
        await session.execute(
            text(
                "insert into lastfm_request (method, params, params_hash, requested_at) "
                "values ('artist.getinfo', '{}'::jsonb, :h, :ts)"
            ),
            {"h": "1" * 64, "ts": datetime.now(UTC)},
        )
        await session.commit()
        usage = await partition_usage(session)

    assert len(usage) == 2, "current month plus one ahead"
    assert sum(item["rows"] for item in usage) == 1
    assert usage[0]["name"].startswith("lastfm_request_")
