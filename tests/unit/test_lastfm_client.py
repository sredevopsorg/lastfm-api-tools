"""Last.fm client: error mapping, retries, archive-first reads, rate limiting."""

from __future__ import annotations

import time
from datetime import timedelta

import httpx
import pytest
import respx
from sqlalchemy import text
from tests.conftest import requires_postgres

from metaedit.adapters.lastfm.client import LastfmClient, reset_shared_buckets
from metaedit.adapters.lastfm.models import (
    ERROR_INVALID_API_KEY,
    ERROR_INVALID_PARAMS,
    ERROR_RATE_LIMIT_EXCEEDED,
    ERROR_SUSPENDED_KEY,
)
from metaedit.adapters.lastfm.ratelimit import TokenBucket
from metaedit.archive.store import ArchiveStore, Observation
from metaedit.config import Settings
from metaedit.domain.errors import (
    LastfmAuthError,
    LastfmNotFound,
    LastfmThrottled,
)

ROOT = "http://ws.audioscrobbler.test/2.0/"


def client_ttl(settings):  # debug helper

    return timedelta(seconds=settings.archive_freshness_ttl_s)


# The params the client actually sends for a name lookup: `autocorrect` is always
# on, so it is part of the request identity used for archive lookups.
CLIENT_PARAMS: dict[str, object] = {"artist": "Cher", "autocorrect": "1"}

CHER = {
    "artist": {
        "name": "Cher",
        "mbid": "bfcc6d75-a6a5-4bc6-8282-47aec8531818",
        "url": "https://www.last.fm/music/Cher",
        "stats": {"listeners": "196440", "plays": "1599101"},
        "tags": {"tag": [{"name": "pop"}]},
        "bio": {"summary": "Cher is an American singer.", "published": "Thu, 13 Mar 2008"},
        "similar": {"artist": [{"name": "Madonna", "match": "1"}]},
    }
}


def _settings(**overrides: object) -> Settings:
    defaults: dict[str, object] = {
        "_env_file": None,
        "LASTFM_API_KEY": "test-lastfm-key",
        "LASTFM_API_ROOT": ROOT,
        "LASTFM_MAX_RPS": 50.0,
        "LASTFM_BURST": 50,
        "LASTFM_MAX_RETRIES": 2,
        # Mirrors the application default: archiving is on unless a test opts out.
        "ARCHIVE_ENABLED": True,
        "LOG_JSON": False,
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _clean_process_state() -> None:
    """Shared token buckets and read counters are process-wide: reset per test."""
    from metaedit.archive.stats import reset_read_metrics

    reset_shared_buckets()
    reset_read_metrics()
    yield
    reset_read_metrics()


@pytest.fixture
def router() -> respx.Router:
    with respx.mock(assert_all_called=False, base_url=ROOT) as mock_router:
        yield mock_router


async def test_artist_info_maps_the_documented_fields(router: respx.Router) -> None:
    router.get(ROOT).respond(200, json=CHER)
    async with LastfmClient(_settings()) as client:
        artist, result = await client.artist_info(artist="Cher")
    assert artist.name == "Cher"
    assert artist.stats is not None
    assert artist.stats.listeners == 196440
    assert result.served_from_archive is False
    assert result.body == CHER


async def test_request_identifies_itself_with_a_user_agent(router: respx.Router) -> None:
    """Last.fm requires an identifiable User-Agent and may ban anonymous callers."""
    route = router.get(ROOT).respond(200, json=CHER)
    async with LastfmClient(_settings()) as client:
        await client.artist_info(artist="Cher")
    assert route.calls[0].request.headers["User-Agent"]


async def test_request_sends_api_key_and_json_format(router: respx.Router) -> None:
    route = router.get(ROOT).respond(200, json=CHER)
    async with LastfmClient(_settings()) as client:
        await client.artist_info(artist="Cher")
    params = route.calls[0].request.url.params
    assert params["api_key"] == "test-lastfm-key"
    assert params["format"] == "json"
    assert params["method"] == "artist.getinfo"
    assert params["artist"] == "Cher"


async def test_mbid_lookup_prefers_the_mbid_param(router: respx.Router) -> None:
    route = router.get(ROOT).respond(200, json=CHER)
    async with LastfmClient(_settings()) as client:
        await client.artist_info(mbid="bfcc6d75", artist="ignored")
    params = route.calls[0].request.url.params
    assert params["mbid"] == "bfcc6d75"
    # Both are sent; Last.fm prefers mbid. The archive records what was asked.
    assert "artist" in params


async def test_no_api_key_fails_before_any_request(router: respx.Router) -> None:
    async with LastfmClient(_settings(LASTFM_API_KEY="")) as client:
        with pytest.raises(LastfmAuthError, match=r"No Last.fm API key"):
            await client.artist_info(artist="Cher")
    assert not router.calls


@pytest.mark.parametrize("code", [ERROR_INVALID_API_KEY, ERROR_SUSPENDED_KEY])
async def test_fatal_error_codes_are_not_retried(router: respx.Router, code: int) -> None:
    route = router.get(ROOT).respond(200, json={"error": code, "message": "bad key"})
    async with LastfmClient(_settings()) as client:
        with pytest.raises(LastfmAuthError) as excinfo:
            await client.artist_info(artist="Cher")
    assert excinfo.value.lastfm_code == code
    assert len(route.calls) == 1, "a suspended or invalid key must not be retried"


@pytest.mark.parametrize("code", [ERROR_INVALID_PARAMS, 7])
async def test_not_found_error_codes_map_to_not_found(router: respx.Router, code: int) -> None:
    """Error 6/7 is a normal outcome that drives candidate fallback."""
    route = router.get(ROOT).respond(200, json={"error": code, "message": "no such artist"})
    async with LastfmClient(_settings()) as client:
        with pytest.raises(LastfmNotFound) as excinfo:
            await client.artist_info(artist="Nobody")
    assert excinfo.value.lastfm_code == code
    assert len(route.calls) == 1


async def test_rate_limit_error_is_retried_then_succeeds(router: respx.Router) -> None:
    route = router.get(ROOT)
    route.side_effect = [
        httpx.Response(200, json={"error": ERROR_RATE_LIMIT_EXCEEDED, "message": "slow down"}),
        httpx.Response(200, json=CHER),
    ]
    async with LastfmClient(_settings(LASTFM_MAX_RETRIES=2)) as client:
        artist, _ = await client.artist_info(artist="Cher")
    assert artist.name == "Cher"
    assert len(route.calls) == 2


async def test_rate_limit_error_gives_up_after_the_retry_budget(router: respx.Router) -> None:
    route = router.get(ROOT).respond(
        200, json={"error": ERROR_RATE_LIMIT_EXCEEDED, "message": "slow down"}
    )
    async with LastfmClient(_settings(LASTFM_MAX_RETRIES=1)) as client:
        with pytest.raises(LastfmThrottled) as excinfo:
            await client.artist_info(artist="Cher")
    assert excinfo.value.lastfm_code == ERROR_RATE_LIMIT_EXCEEDED
    assert len(route.calls) == 2


async def test_http_5xx_is_retried(router: respx.Router) -> None:
    route = router.get(ROOT)
    route.side_effect = [
        httpx.Response(503, headers={"Retry-After": "0"}),
        httpx.Response(200, json=CHER),
    ]
    async with LastfmClient(_settings()) as client:
        artist, _ = await client.artist_info(artist="Cher")
    assert artist.name == "Cher"
    assert len(route.calls) == 2


async def test_non_json_body_is_retried_then_reported(router: respx.Router) -> None:
    route = router.get(ROOT).respond(200, html="<html>maintenance</html>")
    async with LastfmClient(_settings(LASTFM_MAX_RETRIES=1)) as client:
        with pytest.raises(LastfmThrottled, match="non-JSON"):
            await client.artist_info(artist="Cher")
    assert len(route.calls) == 2


async def test_timeout_is_retried_then_reported(router: respx.Router) -> None:
    route = router.get(ROOT).mock(side_effect=httpx.ConnectTimeout("nope"))
    async with LastfmClient(_settings(LASTFM_MAX_RETRIES=1)) as client:
        with pytest.raises(LastfmThrottled, match="timed out"):
            await client.artist_info(artist="Cher")
    assert len(route.calls) == 2


async def test_search_returns_candidates_for_disambiguation(router: respx.Router) -> None:
    router.get(ROOT).respond(
        200,
        json={
            "results": {
                "artistmatches": {
                    "artist": [
                        {"name": "Cher", "mbid": "m1", "listeners": "100"},
                        {"name": "Cher Lloyd", "mbid": "m2", "listeners": "50"},
                    ]
                }
            }
        },
    )
    async with LastfmClient(_settings()) as client:
        results, _ = await client.artist_search("cher")
    assert [result.name for result in results] == ["Cher", "Cher Lloyd"]


# --------------------------------------------------------------- rate limiting


async def test_token_bucket_allows_a_burst_then_throttles() -> None:
    bucket = TokenBucket(rate_per_second=20.0, burst=2)
    start = time.monotonic()
    for _ in range(4):
        await bucket.acquire()
    elapsed = time.monotonic() - start
    # Two tokens are free; the next two need ~0.05s each at 20/s.
    assert elapsed >= 0.08, f"burst was not bounded: elapsed={elapsed:.3f}s"
    assert elapsed < 1.0


async def test_token_bucket_refuses_a_nonsense_rate() -> None:
    with pytest.raises(ValueError, match="must be positive"):
        TokenBucket(rate_per_second=0, burst=1)


async def test_token_bucket_reports_availability() -> None:
    bucket = TokenBucket(rate_per_second=10.0, burst=1)
    assert bucket.time_until_available() == 0.0
    await bucket.acquire()
    assert bucket.time_until_available() > 0.0


async def test_client_rate_is_enforced_across_calls(router: respx.Router) -> None:
    """The bucket is shared per process, so sequential calls are paced too."""
    router.get(ROOT).respond(200, json=CHER)
    settings = _settings(LASTFM_MAX_RPS=20.0, LASTFM_BURST=1)
    reset_shared_buckets()
    start = time.monotonic()
    async with LastfmClient(settings) as client:
        for _ in range(3):
            await client.artist_info(artist="Cher")
    elapsed = time.monotonic() - start
    assert elapsed >= 0.08, f"3 calls at 20/s with burst 1 cannot happen faster: {elapsed:.3f}s"


# ------------------------------------------------------- archive-first reads


@requires_postgres
async def test_archive_first_skips_the_network(database_url: str) -> None:
    """A fresh stored response must be served with zero API calls."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from metaedit.db.partitions import ensure_partitions

    engine = create_async_engine(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    with respx.mock(assert_all_called=False, base_url=ROOT) as router:
        route = router.get(ROOT).respond(200, json={"artist": {"name": "CHANGED"}})
        try:
            async with factory() as session:
                await ensure_partitions(await session.connection(), months_ahead=1)
                store = ArchiveStore(session, _settings())
                await store.record(_observation("artist.getinfo", CLIENT_PARAMS, CHER))
                await session.commit()

                async with LastfmClient(_settings(), store=store) as client:
                    artist, result = await client.artist_info(artist="Cher")
                await session.commit()

            assert result.served_from_archive is True
            assert artist.name == "Cher", "the stored body must win over anything remote"
            assert result.response_id is not None
            assert not route.called, "a fresh archive hit must not spend a request"
        finally:
            await engine.dispose()


@requires_postgres
async def test_archive_hit_is_not_logged_as_a_request(database_url: str) -> None:
    """Regression: an archive hit must not appear in the attempt log.

    ``lastfm_request`` is the append-only log of HTTP attempts to Last.fm -- it
    backs ``request_rows``, the per-partition counts, and any rate-limit or
    failure analysis. An archive hit is exactly the case where no attempt was
    made, so a row there would make those numbers lie. Reads are counted in
    process memory instead.
    """
    from sqlalchemy import func, select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from metaedit.archive.stats import read_metrics, reset_read_metrics
    from metaedit.db.models import LastfmRequest
    from metaedit.db.partitions import ensure_partitions

    reset_read_metrics()
    engine = create_async_engine(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    with respx.mock(assert_all_called=False, base_url=ROOT) as router:
        router.get(ROOT).respond(200, json={"artist": {"name": "SHOULD NOT BE USED"}})
        try:
            async with factory() as session:
                await ensure_partitions(await session.connection(), months_ahead=1)
                store = ArchiveStore(session, _settings())
                await store.record(_observation("artist.getinfo", CLIENT_PARAMS, CHER))
                await session.commit()
                requests_after_fetch = await session.scalar(
                    select(func.count()).select_from(LastfmRequest)
                )

                # Three reads, all served from the archive.
                async with LastfmClient(_settings(), store=store) as client:
                    for _ in range(3):
                        artist, result = await client.artist_info(artist="Cher")
                        assert result.served_from_archive is True
                await session.commit()
                requests_after_reads = await session.scalar(
                    select(func.count()).select_from(LastfmRequest)
                )
                stray = await session.scalar(
                    select(func.count())
                    .select_from(LastfmRequest)
                    .where(LastfmRequest.served_from_archive)
                )

            assert artist.name == "Cher"
            assert requests_after_reads == requests_after_fetch == 1, (
                "reads must not add rows to the attempt log"
            )
            assert stray == 0, "no row may claim to be an archive read"
            counts = read_metrics()
            assert counts.hits == 3, "reads are counted in memory instead"
            assert counts.misses == 0
        finally:
            await engine.dispose()
    reset_read_metrics()


@requires_postgres
async def test_archive_miss_is_counted_and_fetches(database_url: str) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from metaedit.archive.stats import read_metrics, reset_read_metrics
    from metaedit.db.partitions import ensure_partitions

    reset_read_metrics()
    engine = create_async_engine(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    with respx.mock(assert_all_called=False, base_url=ROOT) as router:
        route = router.get(ROOT).respond(200, json=CHER)
        try:
            async with factory() as session:
                await ensure_partitions(await session.connection(), months_ahead=1)
                store = ArchiveStore(session, _settings())
                async with LastfmClient(_settings(), store=store) as client:
                    await client.artist_info(artist="Cher")
                await session.commit()

            assert route.called, "an empty archive must go to the network"
            counts = read_metrics()
            assert counts.misses == 1
            assert counts.hits == 0
            assert counts.hit_ratio == 0.0
        finally:
            await engine.dispose()
    reset_read_metrics()


@requires_postgres
async def test_archive_ignores_a_stale_response(database_url: str) -> None:
    """Beyond the freshness window the archive must refresh from Last.fm."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from metaedit.db.partitions import ensure_partitions

    engine = create_async_engine(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    with respx.mock(assert_all_called=False, base_url=ROOT) as router:
        route = router.get(ROOT).respond(200, json={"artist": {"name": "Fresh"}})
        try:
            async with factory() as session:
                await ensure_partitions(await session.connection(), months_ahead=1)
                store = ArchiveStore(session, _settings())
                await store.record(_observation("artist.getinfo", CLIENT_PARAMS, CHER))
                # Backdate beyond the freshness window. Only last_seen_at drives
                # freshness, so requested_at is left alone: moving it outside the
                # created partitions would test partitioning, not staleness.
                await session.execute(
                    text("update lastfm_response set last_seen_at = now() - interval '1 hour'")
                )
                await session.commit()

                settings = _settings(ARCHIVE_ENABLED=True, ARCHIVE_FRESHNESS_TTL_S=60)
                async with LastfmClient(settings, store=store) as client:
                    artist, result = await client.artist_info(artist="Cher")
                await session.commit()

            assert route.called, "stale data must be refreshed from the network"
            assert artist.name == "Fresh"
            assert result.served_from_archive is False
            # Both bodies are now retained, which is what makes change detection possible.
            distinct = await _distinct_responses(engine, "artist.getinfo", CLIENT_PARAMS)
            assert distinct == 2
        finally:
            await engine.dispose()


async def _distinct_responses(engine: object, method: str, params: dict[str, object]) -> int:
    from sqlalchemy import func, select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from metaedit.adapters.lastfm.canonical import params_hash
    from metaedit.db.models import LastfmRequest

    factory = async_sessionmaker(engine, expire_on_commit=False)  # type: ignore[arg-type]
    async with factory() as session:
        stmt = select(func.count(func.distinct(LastfmRequest.response_id))).where(
            LastfmRequest.params_hash == params_hash(method, params)
        )
        return int(await session.scalar(stmt) or 0)


async def test_name_lookup_identity_matches_what_is_stored(router: respx.Router) -> None:
    """Regression: a lookup must hash exactly as the stored request did.

    The client always sends ``autocorrect``, so it is part of the request
    identity. When it was only added at the HTTP layer, the archive lookup hashed
    a different string from the stored row and silently missed every time.
    """
    route = router.get(ROOT).respond(200, json=CHER)
    async with LastfmClient(_settings()) as client:
        await client.artist_info(artist="Cher")
    sent = route.calls[0].request.url.params
    # Everything sent over the wire that could change the response must be part
    # of the identity used for archive lookups.
    from metaedit.adapters.lastfm.canonical import canonical_params

    identity = canonical_params("artist.getinfo", {"artist": "Cher", "autocorrect": "1"})
    assert identity["autocorrect"] == sent["autocorrect"]


def _observation(method: str, params: dict[str, object], body: dict[str, object]) -> Observation:
    return Observation(
        method=method,
        params=params,
        http_status=200,
        duration_ms=10,
        body=body,
        user_agent="test",
    )
