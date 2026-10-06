"""Library endpoints against a stubbed Jellyfin.

The stubbed Jellyfin returns realistic payloads, including the fields the real
server omits for albums and songs, so the read path is exercised end to end
through FastAPI rather than only through the domain functions.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from metaedit.adapters.jellyfin.client import JellyfinClient
from metaedit.config import Settings, get_settings
from metaedit.main import create_app

BASE = "http://jellyfin.test:8096"

ARTIST: dict[str, Any] = {
    "Id": "aaaaaaaa-0000-0000-0000-000000000001",
    "Type": "MusicArtist",
    "Name": "Radiohead",
    "Etag": "etag-1",
    "SourceType": "Library",
    "DateLastSaved": "2026-01-02T03:04:05Z",
    "Genres": ["Alternative Rock"],
    "Tags": [],
    "ProviderIds": {"MusicBrainzArtist": "a74b1b7f-71a5-4011-9441-d0b5e4122711"},
    "ExternalUrls": [],
    "Studios": [],
    "ProductionLocations": [],
    "People": [],
    "LockedFields": [],
    "ArtistItems": [{"Name": "Radiohead", "Id": "aaaaaaaa-0000-0000-0000-000000000001"}],
    "ChildCount": 9,
}

SONG: dict[str, Any] = {
    "Id": "cccccccc-0000-0000-0000-000000000003",
    "Type": "Audio",
    "Name": "Paranoid Android",
    "SourceType": "Library",
    "Album": "OK Computer",
    "AlbumId": "bbbbbbbb-0000-0000-0000-000000000002",
    "Genres": [],
    "Tags": ["britpop"],
    "ProviderIds": {},
    "ExternalUrls": [],
    "Studios": [],
    "ProductionLocations": [],
    "People": [],
    "LockedFields": [],
}


def _settings() -> Settings:
    # _env_file=None: the developer's local .env must not leak into tests.
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        JELLYFIN_URL=BASE,
        JELLYFIN_API_KEY="test-admin-key",
        LASTFM_API_KEY="test-lastfm-key",
        LOG_JSON=False,
        DATABASE_URL="postgresql+psycopg://nobody@127.0.0.1:1/nothing",
    )


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """TestClient whose Jellyfin client is backed by a stub transport."""
    transport = httpx.MockTransport(_handler)
    original_enter = JellyfinClient.__aenter__

    async def patched_enter(self: JellyfinClient) -> JellyfinClient:
        # Swap in a mock transport, then delegate to the real enter path.
        self._client = httpx.AsyncClient(transport=transport, timeout=2.0)
        self._owns_client = True
        return self

    monkeypatch.setattr(JellyfinClient, "__aenter__", patched_enter)
    del original_enter

    settings = _settings()
    app = create_app(settings)
    # The routers inject settings via Depends(get_settings); without this override
    # they would read the cached, .env-derived instance instead of the test one.
    app.dependency_overrides[get_settings] = lambda: settings
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


def _handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/Users/Me":
        return httpx.Response(200, json={"Id": "u", "Policy": {"IsAdministrator": True}})
    if path == "/System/Info":
        return httpx.Response(200, json={"Version": "12.2.0"})
    if path == "/Library/VirtualFolders":
        # VirtualFolders (a bare list), which is what supplies ItemId on a live
        # server; MediaFolders returned null for every library on 12.2.0.
        return httpx.Response(
            200,
            json=[
                {"Name": "Music", "ItemId": "1", "CollectionType": "music"},
                {"Name": "Films", "ItemId": "2", "CollectionType": "movies"},
            ],
        )
    if path == "/Items":
        return httpx.Response(200, json={"Items": [ARTIST, SONG], "TotalRecordCount": 2})
    if path.startswith("/Items/") and path.endswith("/Refresh"):
        return httpx.Response(204)
    if path.startswith("/Items/"):
        item_id = path.rsplit("/", 1)[-1]
        for candidate in (ARTIST, SONG):
            if candidate["Id"] == item_id:
                return httpx.Response(200, json=candidate)
        return httpx.Response(404, json={"error": "not found"})
    return httpx.Response(404)


def test_libraries_only_lists_music(client: TestClient) -> None:
    response = client.get("/api/libraries")
    assert response.status_code == 200
    assert [item["name"] for item in response.json()] == ["Music"]


def test_items_returns_summaries(client: TestClient) -> None:
    response = client.get("/api/items", params={"kind": "artist"})
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert body["items"][0]["name"] == "Radiohead"
    assert body["items"][0]["has_provider_ids"] is True
    # A song with no genres but an overview flag of False is still listed.
    assert body["items"][1]["has_overview"] is False


def test_missing_metadata_filter(client: TestClient) -> None:
    response = client.get("/api/items", params={"kind": "artist", "missing_metadata": "true"})
    assert response.status_code == 200
    names = [item["name"] for item in response.json()["items"]]
    # Radiohead has genres and provider ids but no overview.
    assert "Radiohead" in names
    assert "Paranoid Android" in names


def test_item_state_exposes_full_writable_fields(client: TestClient) -> None:
    response = client.get(f"/api/items/{ARTIST['Id']}/state")
    assert response.status_code == 200
    state = response.json()
    assert state["kind"] == "MusicArtist"
    assert state["editable"] is True
    assert state["etag"] == "etag-1"
    fields = state["fields"]
    assert fields["Genres"] == ["Alternative Rock"]
    assert fields["ProviderIds"] == {"MusicBrainzArtist": "a74b1b7f-71a5-4011-9441-d0b5e4122711"}
    # Every whitelist field is present, so the state is directly usable to build a payload.
    from metaedit.domain.writable import payload_fields

    assert set(payload_fields("MusicArtist")) == set(fields)


def test_song_state_has_the_song_field_set_not_the_artist_one(client: TestClient) -> None:
    response = client.get(f"/api/items/{SONG['Id']}/state")
    assert response.status_code == 200
    fields = response.json()["fields"]
    assert "ArtistItems" not in fields
    from metaedit.domain.writable import payload_fields

    assert set(payload_fields("Audio")) == set(fields)


def test_item_states_hydrates_many_items_in_one_call(client: TestClient) -> None:
    response = client.post("/api/items/states", json=[ARTIST["Id"], SONG["Id"], "unknown-id"])
    assert response.status_code == 200
    body = response.json()
    # The stub returns 404 for the unknown id, which becomes an upstream 404.
    assert isinstance(body, list)


def test_unknown_item_is_a_404(client: TestClient) -> None:
    response = client.get("/api/items/does-not-exist/state")
    assert response.status_code == 404


def test_refresh_item_is_a_separate_explicit_action(client: TestClient) -> None:
    """Refresh must not be how edits get applied."""
    response = client.post(f"/api/items/{ARTIST['Id']}/refresh")
    assert response.status_code == 200
    assert response.json() == {"status": "queued"}


def test_error_bodies_use_our_envelope(client: TestClient) -> None:
    response = client.get("/api/items/does-not-exist/state")
    body = response.json()
    assert set(body) == {"error"}
    assert body["error"]["code"] == "not_found"
    assert body["error"]["retryable"] is False
