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


@pytest.fixture
def seen_requests(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    """Every Jellyfin request the app makes, in order.

    Asserting on the outbound query is the only way to test sort and filter translation:
    the response has the same shape either way, and Jellyfin *ignores* an unknown sort
    key, so a test that only read the response could not tell a working sort from a
    silently dropped one. That is the failure mode this feature exists to prevent, so
    the tests read the request instead.
    """
    seen: list[httpx.Request] = []

    def recording_handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _handler(request)

    transport = httpx.MockTransport(recording_handler)

    async def patched_enter(self: JellyfinClient) -> JellyfinClient:
        self._client = httpx.AsyncClient(transport=transport, timeout=2.0)
        self._owns_client = True
        return self

    monkeypatch.setattr(JellyfinClient, "__aenter__", patched_enter)

    settings = _settings()
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    with TestClient(app, raise_server_exceptions=False) as test_client:
        # The client is reachable from the test through `app.state`, but tests only need
        # to issue requests; exposing it would let them bypass the real handler stack.
        app.state.test_client = test_client
        yield seen


def _browse_query(seen: list[httpx.Request]) -> dict[str, str]:
    """The query of the last ``/Items`` browse, as a plain dict."""
    browse = [r for r in seen if r.url.path == "/Items"]
    assert browse, f"no /Items request; saw {[r.url.path for r in seen]}"
    return dict(browse[-1].url.params)


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


# ------------------------------------------------------- sort and filter translation


def test_the_browse_query_defaults_are_sent_explicitly(
    client: TestClient, seen_requests: list[httpx.Request]
) -> None:
    """No default is left to the server's discretion.

    Jellyfin sorts by `SortName` when `sortBy` is absent, but relying on that would make
    our ordering a property of someone else's implementation.
    """
    client.get("/api/items")
    query = _browse_query(seen_requests)
    assert query["sortBy"] == "SortName"
    assert query["sortOrder"] == "Ascending"


@pytest.mark.parametrize(
    ("sort", "order", "expected_key", "expected_order"),
    [
        ("name", "asc", "Name", "Ascending"),
        ("sort_name", "asc", "SortName", "Ascending"),
        ("date_added", "desc", "DateCreated", "Descending"),
        ("year", "desc", "ProductionYear", "Descending"),
        ("random", "desc", "Random", "Ascending"),
    ],
)
def test_every_sort_key_reaches_jellyfin_translated(
    client: TestClient,
    seen_requests: list[httpx.Request],
    sort: str,
    order: str,
    expected_key: str,
    expected_order: str,
) -> None:
    response = client.get(f"/api/items?sort={sort}&order={order}")
    assert response.status_code == 200
    query = _browse_query(seen_requests)
    assert query["sortBy"] == expected_key
    assert query["sortOrder"] == expected_order


def test_an_unknown_sort_key_is_refused_rather_than_ignored(
    client: TestClient, seen_requests: list[httpx.Request]
) -> None:
    """Jellyfin would accept and ignore this, showing one order while claiming another.

    Refusing at the boundary is the only place the mistake can be caught: once the
    request is out, a wrong order is indistinguishable from a right one.
    """
    response = client.get("/api/items?sort=release_year")
    assert response.status_code == 422
    assert not [r for r in seen_requests if r.url.path == "/Items"]


def test_an_unknown_order_is_refused(client: TestClient) -> None:
    assert client.get("/api/items?order=sideways").status_code == 422


def test_server_filters_reach_jellyfin(
    client: TestClient, seen_requests: list[httpx.Request]
) -> None:
    client.get("/api/items?has_overview=false&year=1995")
    query = _browse_query(seen_requests)
    assert query["hasOverview"] == "false"
    assert query["Years"] == "1995"


def test_absent_filters_are_absent_from_the_request(
    client: TestClient, seen_requests: list[httpx.Request]
) -> None:
    """An empty filter parameter is not the same as no filter, and must not be sent."""
    client.get("/api/items")
    query = _browse_query(seen_requests)
    assert "hasOverview" not in query
    assert "Years" not in query


def test_has_overview_accepts_false_as_a_value_not_an_absence(
    client: TestClient, seen_requests: list[httpx.Request]
) -> None:
    """`false` is a filter -- "items lacking an overview" -- and used to be unfilterable."""
    client.get("/api/items?has_overview=false")
    assert _browse_query(seen_requests)["hasOverview"] == "false"


def test_paging_is_translated(client: TestClient, seen_requests: list[httpx.Request]) -> None:
    client.get("/api/items?start_index=100&page_size=25")
    query = _browse_query(seen_requests)
    assert query["startIndex"] == "100"
    assert query["limit"] == "25"


def test_the_response_reports_what_was_applied(client: TestClient) -> None:
    """The UI renders what the server did, not what it asked for."""
    body = client.get("/api/items?sort=year&order=desc&page_size=25").json()
    assert body["sort"] == "year"
    assert body["order"] == "desc"
    assert body["page_size"] == 25
    assert body["start_index"] == 0


def test_a_page_filter_says_it_only_filtered_the_page(client: TestClient) -> None:
    """The bug this field exists for.

    `missing_metadata` cannot be expressed to Jellyfin, so it narrows the fetched page
    while `total` keeps counting everything. The old UI showed the unfiltered total above
    a filtered table with nothing saying the two disagreed.
    """
    body = client.get("/api/items?missing_metadata=true").json()
    assert body["filtered_client_side"] is True
    # The unfiltered count is still the honest count of the query, not of the table.
    assert body["total"] == 2


def test_a_server_side_filter_does_not_claim_to_be_client_side(client: TestClient) -> None:
    body = client.get("/api/items?has_overview=false").json()
    assert body["filtered_client_side"] is False
