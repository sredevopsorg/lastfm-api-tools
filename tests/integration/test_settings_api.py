"""The settings endpoints, against a real database and a stubbed Jellyfin.

The blacklist is only useful if saving it changes what a *diff* proposes, so these go all
the way through FastAPI: PUT the setting, then POST a diff and assert the genre is gone.
A test that only read the setting back would pass while the wiring was disconnected --
which is precisely the failure this feature replaces, where the value existed but the
mapping layer never consulted it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from tests.conftest import requires_postgres

from metaedit.adapters.jellyfin.client import JellyfinClient
from metaedit.config import Settings, get_settings
from metaedit.db.session import dispose_engine
from metaedit.main import create_app

pytestmark = requires_postgres

BASE = "http://jellyfin.test:8096"
ARTIST_ID = "aaaaaaaa-0000-0000-0000-000000000001"

# An artist whose stored genres include a comma-bearing value, matching the live library
# (8 of 502 albums carry one). The comma cases are the ones most likely to be handled
# wrongly, so the fixture reproduces them rather than using tidy single-word genres.
#
# ``Gothic Rock`` is deliberately absent from this list, and the absence is load-bearing.
# The blacklist governs what may be *added*; it cannot remove a genre the item already has,
# because merge mode unions the existing values through ``_merge``, which does not consult
# the blacklist. A fixture that already carried the blacklisted genre would make these
# tests pass only if the blacklist wrongly pruned curated data -- the opposite of the
# guarantee. ``test_a_blacklisted_genre_already_present_is_left_alone`` pins that boundary.
ARTIST: dict[str, Any] = {
    "Id": ARTIST_ID,
    "Type": "MusicArtist",
    "Name": "Radiohead",
    "Etag": "etag-1",
    "SourceType": "Library",
    "Genres": ["Alternative Rock", "Rock, Reggae", "Gothic"],
    "Tags": ["britpop"],
    "ProviderIds": {},
    "ExternalUrls": [],
    "Studios": [],
    "ProductionLocations": [],
    "People": [],
    "LockedFields": [],
    "ArtistItems": [{"Name": "Radiohead", "Id": ARTIST_ID}],
    "ChildCount": 9,
}


def _handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/Items":
        # One item, whichever ids were asked for: the vocabulary preview reads this
        # endpoint without ids, and the single-item path is handled below.
        return httpx.Response(200, json={"Items": [ARTIST], "TotalRecordCount": 1})
    if path.startswith("/Items/"):
        return httpx.Response(200, json=ARTIST)
    if path == "/Users":
        return httpx.Response(200, json=[{"Id": "user-1", "Policy": {"IsAdministrator": True}}])
    return httpx.Response(404, json={"error": "unexpected"})


def _settings(database_url: str) -> Settings:
    return Settings(
        _env_file=None,
        JELLYFIN_URL=BASE,
        JELLYFIN_API_KEY="test-admin-key",
        LASTFM_API_KEY="test-lastfm-key",
        LOG_JSON=False,
        DATABASE_URL=database_url,
        TAG_BLACKLIST_EXTRA="",
    )


@pytest.fixture
def client(database_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """A TestClient over the *real* test database, with a stubbed Jellyfin.

    The engine must be reset before and after, and that is not defensive tidiness.
    ``init_engine`` is idempotent and module-global: it keeps its engine in a module
    variable and returns early if one exists. Any other test that calls ``create_app``
    therefore leaves an engine pointing at *its* database -- and every test gets a private
    one that is dropped at teardown. Without the reset, whichever test runs second
    inherits a connection pool aimed at a database that no longer exists and fails with
    ``FATAL: database ... does not exist``.

    Other suites dodge this by pointing ``DATABASE_URL`` at an unreachable address so no
    connection is ever made. This one cannot: the whole point is to assert that a saved
    setting reaches the plan through the database.
    """
    transport = httpx.MockTransport(_handler)

    async def patched_enter(self: JellyfinClient) -> JellyfinClient:
        self._client = httpx.AsyncClient(transport=transport, timeout=2.0)
        self._owns_client = True
        return self

    monkeypatch.setattr(JellyfinClient, "__aenter__", patched_enter)

    # Drop any engine left behind by an earlier test before this app initialises its own.
    asyncio.run(dispose_engine())

    settings = _settings(database_url)
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        with TestClient(app, raise_server_exceptions=False) as test_client:
            yield test_client
    finally:
        # Leave the global clean so the *next* test starts from a known state.
        asyncio.run(dispose_engine())


async def _rows(database_url: str) -> list[tuple[str, str]]:
    engine = create_async_engine(database_url)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with maker() as session:
            result = await session.execute(
                text("select value, value_norm from genre_blacklist order by value_norm")
            )
            return [(row[0], row[1]) for row in result.all()]
    finally:
        await engine.dispose()


def _rows_sync(database_url: str) -> list[tuple[str, str]]:
    """Read the rows on a separate sync connection.

    Not ``asyncio.run``: ``TestClient`` drives the application's event loop, so entering
    a second one inside a test raises. A synchronous psycopg connection sidesteps that and
    reads the committed state, which is what these assertions are about.
    """
    import psycopg

    url = database_url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(url) as conn, conn.cursor() as cursor:
        cursor.execute("select value, value_norm from genre_blacklist order by value_norm")
        return [(row[0], row[1]) for row in cursor.fetchall()]


# ------------------------------------------------------------------ reading


def test_an_empty_blacklist_still_reports_the_defaults(client: TestClient) -> None:
    response = client.get("/api/settings/genre-blacklist")
    assert response.status_code == 200

    body = response.json()
    assert body["entries"] == []
    assert "seen live" in body["defaults"]
    assert body["env"] == []
    assert "seen live" in body["effective"]
    assert "case-insensitive" in body["note"]


# -------------------------------------------------------------------- writing


def test_saving_stores_the_entries(client: TestClient) -> None:
    response = client.put("/api/settings/genre-blacklist", json={"raw": "Gothic Rock\nSka"})
    assert response.status_code == 200

    body = response.json()
    assert body["saved"] == ["Gothic Rock", "Ska"]
    assert body["count"] == 2
    assert body["conflicts"] == []
    assert body["needs_review"] is False
    assert body["state"]["entries"] == ["Gothic Rock", "Ska"]


def test_saving_replaces_rather_than_merges(client: TestClient) -> None:
    client.put("/api/settings/genre-blacklist", json={"raw": "Rock\nSka"})
    body = client.put("/api/settings/genre-blacklist", json={"raw": "Reggae"}).json()
    assert body["state"]["entries"] == ["Reggae"]


def test_a_comma_line_is_reported_but_the_rest_is_saved(client: TestClient) -> None:
    """A 200 with ``needs_review``, not a 422: refusing the block would discard the
    entries that were perfectly clear."""
    response = client.put(
        "/api/settings/genre-blacklist", json={"raw": "Gothic\nRock, Reggae\nSka"}
    )
    assert response.status_code == 200

    body = response.json()
    assert body["saved"] == ["Gothic", "Ska"]
    assert body["needs_review"] is True
    assert len(body["conflicts"]) == 1
    assert body["conflicts"][0]["whole"] == "Rock, Reggae"
    assert body["conflicts"][0]["fragments"] == ["Rock", "Reggae"]


def test_allow_commas_saves_every_fragment(client: TestClient) -> None:
    body = client.put(
        "/api/settings/genre-blacklist",
        json={"raw": "rock, reggae", "allow_commas": True},
    ).json()
    assert sorted(body["saved"]) == ["reggae", "rock"]
    assert body["needs_review"] is False


def test_clearing_the_list_keeps_the_defaults(client: TestClient) -> None:
    client.put("/api/settings/genre-blacklist", json={"raw": "Rock"})
    body = client.put("/api/settings/genre-blacklist", json={"raw": ""}).json()
    assert body["state"]["entries"] == []
    assert body["state"]["counts"]["defaults"] > 0


def test_an_empty_body_is_accepted_as_clearing(client: TestClient) -> None:
    assert client.put("/api/settings/genre-blacklist", json={}).status_code == 200


# ------------------------------------------------------------------- preview


def test_preview_names_the_live_genres_an_entry_matches(client: TestClient) -> None:
    body = client.post("/api/settings/genre-blacklist/preview", json={"raw": "gothic"}).json()

    assert body["preview"][0]["matches"] == ["Gothic"]
    assert body["would_blacklist"] == ["gothic"]
    # The vocabulary comes from the stubbed library, so this is a real count.
    assert body["vocabulary_size"] >= 3


def test_preview_reports_a_comma_line_with_its_readings(client: TestClient) -> None:
    body = client.post("/api/settings/genre-blacklist/preview", json={"raw": "Rock, Reggae"}).json()

    assert len(body["conflicts"]) == 1
    # The stub stores exactly this genre, so the conflict should surface the library's
    # own spelling rather than merely echoing the input.
    assert body["conflicts"][0]["whole"] == "Rock, Reggae"
    assert body["conflicts"][0]["fragments"] == ["Rock", "Reggae"]


def test_preview_matches_exactly_not_by_substring(client: TestClient) -> None:
    """``rock`` must not report ``Gothic Rock`` or ``Rock, Reggae`` as matches."""
    body = client.post("/api/settings/genre-blacklist/preview", json={"raw": "rock"}).json()
    assert body["preview"][0]["matches"] == []


def test_preview_writes_nothing(client: TestClient, database_url: str) -> None:
    """Preview is a read. A user experimenting with text must not be changing policy."""
    client.post("/api/settings/genre-blacklist/preview", json={"raw": "Rock\nSka"})
    assert client.get("/api/settings/genre-blacklist").json()["entries"] == []


# ------------------------------------------------------------- the real point


def _seed(database_url: str, tags: list[str]) -> int:
    """Put one archived artist in the database the way production would.

    Through ``ArchiveStore`` and ``reindex`` rather than raw inserts, so the diff reads a
    derived layer produced by the same code path that produces it in production. A
    hand-inserted ``lastfm_artist`` row would let this test pass while a reindex bug left
    the real path broken.

    Returns the archived artist's id, which ``/items/{id}/diff`` takes as ``entity_id``.
    """

    async def run() -> int:
        from sqlalchemy import select

        from metaedit.archive.reindex import reindex
        from metaedit.archive.store import ArchiveStore, Observation
        from metaedit.db.models import LastfmArtist
        from metaedit.db.partitions import ensure_partitions

        engine = create_async_engine(database_url)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        settings = _settings(database_url)
        try:
            async with maker() as session:
                await ensure_partitions(await session.connection(), months_ahead=1)
                store = ArchiveStore(session, settings)
                await store.record(
                    Observation(
                        method="artist.getinfo",
                        params={"artist": "Radiohead", "autocorrect": "1"},
                        http_status=200,
                        duration_ms=5,
                        body={
                            "artist": {
                                "name": "Radiohead",
                                "mbid": "a74b1b7f-71a5-4011-9441-d0b5e4122711",
                                "url": "https://www.last.fm/music/Radiohead",
                                "tags": {"tag": [{"name": tag} for tag in tags]},
                                "bio": {"summary": "x" * 80},
                            }
                        },
                        user_agent="test",
                    )
                )
                await session.commit()
                # `reindex` writes through the session but does not commit -- the caller
                # owns the transaction (see `reindex_with_settings`). Committing is what
                # makes the derived rows visible to the app's own connection, which is a
                # separate one.
                await reindex(session, dry_run=False)
                await session.commit()
                row = (
                    await session.execute(
                        select(LastfmArtist).where(LastfmArtist.name == "Radiohead")
                    )
                ).scalar_one()
                return int(row.id)
        finally:
            await engine.dispose()

    return asyncio.run(run())


def test_a_saved_entry_removes_the_genre_from_the_proposed_diff(
    client: TestClient, database_url: str
) -> None:
    """The wiring that makes the feature real, asserted on the proposal itself.

    Last.fm offers ``gothic rock`` and ``ska``; the operator blacklists the first. The
    diff must propose only ``Ska`` -- if the stored blacklist is not merged into the
    policy, both are proposed and the setting is decorative.
    """
    entity_id = _seed(database_url, ["gothic rock", "ska"])
    client.put("/api/settings/genre-blacklist", json={"raw": "Gothic Rock"})

    response = client.post(f"/api/items/{ARTIST_ID}/diff", json={"entity_id": entity_id})
    assert response.status_code == 200, response.text

    changes = {change["field"]: change for change in response.json()["changes"]}
    genres = changes["Genres"]["proposed"]
    assert "Ska" in genres or "ska" in genres, genres
    assert not any(genre.casefold() == "gothic rock" for genre in genres), genres


def test_the_same_diff_proposes_the_genre_when_it_is_not_blacklisted(
    client: TestClient, database_url: str
) -> None:
    """The control for the test above.

    Without this, an implementation that dropped *every* genre would pass the previous
    assertion while blacklisting nothing at all.
    """
    entity_id = _seed(database_url, ["gothic rock", "ska"])

    response = client.post(f"/api/items/{ARTIST_ID}/diff", json={"entity_id": entity_id})
    assert response.status_code == 200, response.text

    changes = {change["field"]: change for change in response.json()["changes"]}
    genres = [genre.casefold() for genre in changes["Genres"]["proposed"]]
    assert "gothic rock" in genres, genres


def test_blacklisting_is_case_insensitive_end_to_end(client: TestClient, database_url: str) -> None:
    """The operator types ``GOTHIC ROCK``; Last.fm returns ``gothic rock``."""
    entity_id = _seed(database_url, ["gothic rock", "ska"])
    client.put("/api/settings/genre-blacklist", json={"raw": "GOTHIC ROCK"})

    response = client.post(f"/api/items/{ARTIST_ID}/diff", json={"entity_id": entity_id})
    changes = {change["field"]: change for change in response.json()["changes"]}
    genres = [genre.casefold() for genre in changes["Genres"]["proposed"]]
    assert "gothic rock" not in genres, genres


def test_a_blacklisted_genre_already_present_is_left_alone(
    client: TestClient, database_url: str
) -> None:
    """The boundary between the two features, pinned explicitly.

    The blacklist stops a genre being *added*. It does not delete one the item already
    has: merge mode carries existing values through ``_merge``, which deliberately does
    not consult the blacklist. Removing a stored genre is the separate, reviewed,
    revertible operation at ``/api/bulk/remove-genre/*`` -- and a blacklist that silently
    pruned curated data on the next unrelated edit would be a destructive surprise.

    ``Rock, Reggae`` is on the fixture item and is not offered by the archived artist, so
    it can only appear in the proposal by being carried through from current state.
    """
    entity_id = _seed(database_url, ["ska"])
    client.put("/api/settings/genre-blacklist", json={"raw": "rock\nreggae\nrock, reggae"})

    response = client.post(f"/api/items/{ARTIST_ID}/diff", json={"entity_id": entity_id})
    assert response.status_code == 200, response.text

    changes = {change["field"]: change for change in response.json()["changes"]}
    # Carried through as the item's own value, split by `_merge`'s component split.
    assert "Rock" in changes["Genres"]["proposed"], changes["Genres"]["proposed"]
    assert "Alternative Rock" in changes["Genres"]["proposed"]


def test_the_setting_takes_effect_without_a_restart(client: TestClient, database_url: str) -> None:
    """The property the env-var implementation could not provide.

    Two reads in one process, with a save between them, must differ -- otherwise the
    setting needs a restart and the feature has not replaced what it set out to.
    """
    assert client.get("/api/settings/genre-blacklist").json()["entries"] == []
    client.put("/api/settings/genre-blacklist", json={"raw": "Gothic Rock"})
    after = client.get("/api/settings/genre-blacklist").json()

    assert after["entries"] == ["Gothic Rock"]
    assert "gothic rock" in after["effective"]


def test_the_stored_value_and_its_key_differ_by_case(client: TestClient, database_url: str) -> None:
    client.put("/api/settings/genre-blacklist", json={"raw": "GOTHIC rock"})
    rows = _rows_sync(database_url)
    assert rows == [("GOTHIC rock", "gothic rock")]
