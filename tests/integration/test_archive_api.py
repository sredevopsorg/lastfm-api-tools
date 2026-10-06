"""Archive inspection endpoints.

The three derived tables do not share a column set — `LastfmAlbum` has no
`overview`, because album getInfo carries no wiki — so every endpoint here is
exercised **per kind**. A test that only covered artists passed while
`GET /api/archive/entities/album/{id}` raised AttributeError, and the type checker
was what surfaced it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from tests.conftest import requires_postgres

from metaedit.archive.reindex import reindex
from metaedit.archive.store import ArchiveStore, Observation
from metaedit.config import Settings, get_settings
from metaedit.db.partitions import ensure_partitions
from metaedit.db.session import get_session
from metaedit.main import create_app

pytestmark = requires_postgres

ARTIST = {
    "artist": {
        "name": "Cher",
        "mbid": "mbid-cher",
        "url": "https://www.last.fm/music/Cher",
        "stats": {"listeners": "196440", "plays": "1599101"},
        "tags": {"tag": [{"name": "pop", "count": "100"}]},
        "bio": {"summary": "Cher is a singer.", "published": "Thu, 13 Mar 2008"},
    }
}
ALBUM = {
    "album": {
        "name": "Believe",
        "artist": "Cher",
        "mbid": "mbid-believe",
        "releasedate": "6 Apr 1999, 00:00",
        "toptags": {"tag": [{"name": "pop"}]},
        "tracks": {"track": [{"name": "Believe", "duration": 239, "rank": "1"}]},
    }
}
TRACK = {
    "track": {
        "name": "Believe",
        "mbid": "mbid-track",
        "duration": "240000",
        "artist": {"name": "Cher", "mbid": "mbid-cher"},
        "album": {"title": "Believe", "mbid": "mbid-believe", "position": "1"},
        "toptags": {"tag": [{"name": "pop"}]},
        "wiki": {"summary": "A hit single."},
    }
}
SIMILAR = {
    "similarartists": {"artist": [{"name": "Madonna", "mbid": "mbid-madonna", "match": "0.9"}]}
}


def _settings(database_url: str) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        DATABASE_URL=database_url,
        ARCHIVE_ENABLED=True,
        ARCHIVE_LOG_REQUESTS=True,
        LASTFM_API_KEY="test-key",
        LOG_JSON=False,
    )


@pytest.fixture
def client(database_url: str) -> Iterator[TestClient]:
    """A TestClient bound to a migrated, freshly derived database."""
    import asyncio

    settings = _settings(database_url)
    engine = create_async_engine(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def seed() -> None:
        async with factory() as session:
            await ensure_partitions(await session.connection(), months_ahead=1)
            store = ArchiveStore(session, settings)
            plan = [
                ("artist.getinfo", {"artist": "Cher", "autocorrect": "1"}, ARTIST),
                (
                    "album.getinfo",
                    {"artist": "Cher", "album": "Believe", "autocorrect": "1"},
                    ALBUM,
                ),
                ("track.getinfo", {"artist": "Cher", "track": "Believe"}, TRACK),
                ("artist.getsimilar", {"artist": "Cher", "limit": 20}, SIMILAR),
            ]
            for method, params, body in plan:
                await store.record(
                    Observation(
                        method=method,
                        params=params,
                        http_status=200,
                        duration_ms=5,
                        body=body,
                        user_agent="test",
                    )
                )
            await session.commit()
            await reindex(session)
            await session.commit()

    asyncio.run(seed())
    asyncio.run(engine.dispose())

    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings

    async def override_session() -> AsyncIterator[Any]:
        # The endpoint under test uses the app's own engine config; give it one
        # bound to this test's database.
        test_engine = create_async_engine(database_url)
        test_factory = async_sessionmaker(test_engine, expire_on_commit=False)
        async with test_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise
        await test_engine.dispose()

    app.dependency_overrides[get_session] = override_session

    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


# --------------------------------------------------------------- stats


def test_stats_report_derived_counts_and_reads(client: TestClient) -> None:
    payload = client.get("/api/archive/stats").json()
    assert payload["state"] == "ok"
    assert payload["reads"]["scope"] == "process"
    assert payload["stray_archive_reads"] == 0
    assert payload["derived"]["counts"]["lastfm_artist"] == 1
    assert payload["derived"]["counts"]["lastfm_album"] == 1
    assert payload["derived"]["counts"]["lastfm_track"] == 1
    assert payload["derived"]["total"] > 0


# ------------------------------------------------------------ entities list


@pytest.mark.parametrize(
    ("kind", "expected_name"),
    [("artist", "Cher"), ("album", "Believe"), ("track", "Believe")],
)
def test_entity_list_supports_every_kind(client: TestClient, kind: str, expected_name: str) -> None:
    """Parametrised per kind: covering only artists hid an album-only crash."""
    response = client.get("/api/archive/entities", params={"kind": kind})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["name"] == expected_name
    assert body["items"][0]["kind"] == kind


@pytest.mark.parametrize("kind", ["artist", "album", "track"])
def test_entity_list_never_reads_a_missing_column(client: TestClient, kind: str) -> None:
    """`overview` exists for artists and tracks but not albums.

    A blind attribute read made the album path raise AttributeError, which surfaced
    as a 500 rather than as a missing field.
    """
    response = client.get("/api/archive/entities", params={"kind": kind})
    assert response.status_code == 200, response.text
    item = response.json()["items"][0]
    assert "overview" in item, "the field is always present, null where it does not apply"


def test_album_overview_is_null_not_a_crash(client: TestClient) -> None:
    item = client.get("/api/archive/entities", params={"kind": "album"}).json()["items"][0]
    assert item["overview"] is None, "album getInfo has no wiki, so there is nothing to report"


def test_track_overview_comes_from_the_wiki(client: TestClient) -> None:
    item = client.get("/api/archive/entities", params={"kind": "track"}).json()["items"][0]
    assert item["overview"] == "A hit single."


def test_entity_list_search_filters_by_name(client: TestClient) -> None:
    assert client.get("/api/archive/entities", params={"search": "Cher"}).json()["total"] == 1
    assert client.get("/api/archive/entities", params={"search": "Nobody"}).json()["total"] == 0


def test_entity_list_filters_by_tag(client: TestClient) -> None:
    """The offline question the archive exists to answer."""
    assert client.get("/api/archive/entities", params={"tag": "pop"}).json()["total"] == 1
    assert client.get("/api/archive/entities", params={"tag": "POP"}).json()["total"] == 1, (
        "tag matching folds case"
    )
    assert client.get("/api/archive/entities", params={"tag": "nope"}).json()["total"] == 0


def test_entity_list_pagination(client: TestClient) -> None:
    body = client.get("/api/archive/entities", params={"page_size": 1, "page": 1}).json()
    assert body["page_size"] == 1
    assert len(body["items"]) == 1


# ---------------------------------------------------------- entity detail


@pytest.mark.parametrize(
    ("kind", "expected_overview"),
    [("artist", "Cher is a singer."), ("album", None), ("track", "A hit single.")],
)
def test_entity_detail_supports_every_kind(
    client: TestClient, kind: str, expected_overview: str | None
) -> None:
    """The detail endpoint reads kind-specific columns, so it is tested per kind."""
    listing = client.get("/api/archive/entities", params={"kind": kind}).json()
    entity_id = listing["items"][0]["id"]

    response = client.get(f"/api/archive/entities/{kind}/{entity_id}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["kind"] == kind
    assert body["overview"] == expected_overview
    assert body["tags"], "the tag edges are attached"
    assert body["tags"][0]["name"] == "pop"
    assert body["tags"][0]["rank"] == 0


def test_artist_detail_includes_similar_artists(client: TestClient) -> None:
    listing = client.get("/api/archive/entities", params={"kind": "artist"}).json()
    artist_id = listing["items"][0]["id"]
    body = client.get(f"/api/archive/entities/artist/{artist_id}").json()
    assert len(body["similar"]) == 1
    assert body["similar"][0]["name"] == "Madonna"
    assert body["similar"][0]["match"] == pytest.approx(0.9)


def test_album_detail_exposes_the_tracklist(client: TestClient) -> None:
    listing = client.get("/api/archive/entities", params={"kind": "album"}).json()
    album_id = listing["items"][0]["id"]
    body = client.get(f"/api/archive/entities/album/{album_id}").json()
    assert body["extra"]["production_year"] == 1999
    assert body["extra"]["tracklist"][0]["duration_ms"] == 239000


def test_track_detail_exposes_duration_and_album_position(client: TestClient) -> None:
    listing = client.get("/api/archive/entities", params={"kind": "track"}).json()
    track_id = listing["items"][0]["id"]
    body = client.get(f"/api/archive/entities/track/{track_id}").json()
    assert body["extra"]["duration_ms"] == 240000
    assert body["extra"]["album_position"] == 1


def test_unknown_entity_is_a_404_without_a_traceback(client: TestClient) -> None:
    response = client.get("/api/archive/entities/artist/999999")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_unknown_kind_is_rejected(client: TestClient) -> None:
    assert client.get("/api/archive/entities", params={"kind": "nonsense"}).status_code == 422
    assert client.get("/api/archive/entities/nonsense/1").status_code == 422


# ------------------------------------------------------ tags and aliases


def test_tags_endpoint_counts_distinct_entities(client: TestClient) -> None:
    body = client.get("/api/archive/tags").json()
    assert body["total"] == 1, "only 'pop' appears across the archive"
    assert body["tags"][0]["name"] == "pop"
    assert body["tags"][0]["norm"] == "pop"
    assert body["tags"][0]["entity_count"] == 3, "artist, album and track each carry it"


def test_aliases_endpoint_is_empty_without_a_misspelling(client: TestClient) -> None:
    assert client.get("/api/archive/aliases").json()["aliases"] == []


# ------------------------------------------------------------- reindex


def test_reindex_defaults_to_dry_run(client: TestClient) -> None:
    """An HTTP client must not rebuild the structured layer by accident."""
    body = client.post("/api/archive/reindex", json={}).json()
    assert body["dry_run"] is True
    assert body["changes"]["identical"] is True


def test_reindex_dry_run_writes_nothing(client: TestClient) -> None:
    before = client.get("/api/archive/stats").json()["derived"]["counts"]
    client.post("/api/archive/reindex", json={})
    after = client.get("/api/archive/stats").json()["derived"]["counts"]
    assert before == after


def test_reindex_can_be_asked_to_write(client: TestClient) -> None:
    body = client.post("/api/archive/reindex", json={"dry_run": False}).json()
    assert body["dry_run"] is False
    assert body["counts"]["artists"] == 1
    # artist.getsimilar carries no entity envelope and is *expected* to be
    # envelope-free, so it must not inflate the count an operator alerts on.
    assert body["counts"]["unexpected_shapes"] == 0
    assert body["counts"]["expected_no_envelope"] == 1
    assert client.get("/api/archive/stats").json()["derived"]["counts"]["lastfm_artist"] == 1


def test_reindex_rejects_an_unknown_only_target(client: TestClient) -> None:
    assert client.post("/api/archive/reindex", json={"only": "nonsense"}).status_code == 422


# ----------------------------------------------------------- diagnosis


def test_diagnose_separates_expected_from_unexpected_shapes(client: TestClient) -> None:
    """Zero unexpected is the healthy value an operator can alert on."""
    body = client.get("/api/archive/diagnose").json()
    assert body["shapes"]["unexpected"] == 0
    assert body["shapes"]["expected_no_envelope"] == 1, "artist.getsimilar"
    assert body["would_derive"]["artists"] == 1
    assert body["raw"]["by_method"]["artist.getsimilar"] == 1
