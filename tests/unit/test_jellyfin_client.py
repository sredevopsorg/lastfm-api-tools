"""Jellyfin HTTP client behaviour: auth, retries, error mapping, request shape."""

from __future__ import annotations

import httpx
import pytest
import respx

from metaedit.adapters.jellyfin.client import ITEM_FIELDS, JellyfinClient
from metaedit.config import Settings
from metaedit.domain.errors import (
    NotFoundError,
    UpstreamAuthError,
    UpstreamContractError,
    UpstreamTimeout,
    UpstreamUnavailable,
)

BASE = "http://jellyfin.test:8096"


def _settings(**overrides: object) -> Settings:
    defaults: dict[str, object] = {
        "_env_file": None,
        "JELLYFIN_URL": BASE,
        "JELLYFIN_API_KEY": "test-admin-key",
        "JELLYFIN_TIMEOUT_S": 1.0,
        "JELLYFIN_MAX_RETRIES": 2,
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


@pytest.fixture
def mock_router() -> respx.Router:
    with respx.mock(assert_all_called=False) as router:
        yield router


async def test_system_info_public_needs_no_auth_and_sends_no_header(
    mock_router: respx.Router,
) -> None:
    route = mock_router.get(f"{BASE}/System/Info/Public").respond(
        200, json={"ServerName": "home", "Version": "10.11.0"}
    )
    async with JellyfinClient(_settings(JELLYFIN_API_KEY="")) as client:
        info = await client.system_info_public()
    assert info.Version == "10.11.0"
    assert "Authorization" not in route.calls[0].request.headers
    assert "X-Emby-Token" not in route.calls[0].request.headers


async def test_item_state_requests_every_whitelist_field(mock_router: respx.Router) -> None:
    """A field we do not ask for is a field we cannot round-trip safely."""
    route = mock_router.get(f"{BASE}/Items/abc").respond(200, json={"Id": "abc", "Name": "x"})
    async with JellyfinClient(_settings()) as client:
        await client.item("abc")
    requested = route.calls[0].request.url.params["fields"].split(",")
    for field in ("Genres", "Tags", "ProviderIds", "ExternalUrls", "Overview", "Studios", "People"):
        assert field in requested, f"{field} must be requested so it can be round-tripped"
    assert set(ITEM_FIELDS).issubset(set(requested))


async def test_api_key_is_sent(mock_router: respx.Router) -> None:
    route = mock_router.get(f"{BASE}/Users/Me").respond(200, json={"Id": "u", "Name": "admin"})
    async with JellyfinClient(_settings()) as client:
        await client.current_user()
    request = route.calls[0].request
    assert request.headers.get("Authorization") == "test-admin-key"


async def test_rejected_key_maps_to_auth_error_with_guidance(mock_router: respx.Router) -> None:
    mock_router.get(f"{BASE}/Items/abc").respond(401)
    async with JellyfinClient(_settings()) as client:
        with pytest.raises(UpstreamAuthError) as excinfo:
            await client.item("abc")
    assert "administrator" in excinfo.value.message
    assert excinfo.value.upstream_status == 401


async def test_missing_key_fails_before_any_request(mock_router: respx.Router) -> None:
    async with JellyfinClient(_settings(JELLYFIN_API_KEY="")) as client:
        with pytest.raises(UpstreamAuthError, match="No Jellyfin API key"):
            await client.item("abc")
    assert not mock_router.calls


async def test_not_found_maps_to_not_found(mock_router: respx.Router) -> None:
    mock_router.get(f"{BASE}/Items/missing").respond(404)
    async with JellyfinClient(_settings()) as client:
        with pytest.raises(NotFoundError):
            await client.item("missing")


async def test_bad_request_is_a_contract_error_that_does_not_leak_the_body(
    mock_router: respx.Router,
) -> None:
    mock_router.post(f"{BASE}/Items/abc").respond(400, text="internal stack trace here")
    async with JellyfinClient(_settings()) as client:
        with pytest.raises(UpstreamContractError) as excinfo:
            await client.update_item("abc", {"Name": "x"})
    # Captured server-side for the logs...
    assert "internal stack trace" in (excinfo.value.detail or "")
    # ...but not part of what a client would see.
    assert "internal stack trace" not in excinfo.value.message


async def test_503_is_retried_with_backoff_honouring_retry_after(mock_router: respx.Router) -> None:
    route = mock_router.get(f"{BASE}/Items/abc")
    route.side_effect = [
        httpx.Response(503, headers={"Retry-After": "0"}),
        httpx.Response(200, json={"Id": "abc", "Name": "recovered"}),
    ]
    async with JellyfinClient(_settings()) as client:
        item = await client.item("abc")
    assert item.Name == "recovered"
    assert len(route.calls) == 2


async def test_503_gives_up_after_the_retry_budget(mock_router: respx.Router) -> None:
    route = mock_router.get(f"{BASE}/Items/abc")
    route.side_effect = httpx.Response(503, headers={"Retry-After": "0"})
    async with JellyfinClient(_settings(JELLYFIN_MAX_RETRIES=1)) as client:
        with pytest.raises(UpstreamUnavailable):
            await client.item("abc")
    assert len(route.calls) == 2, "one attempt plus one retry"


async def test_timeout_maps_to_timeout_error(mock_router: respx.Router) -> None:
    mock_router.get(f"{BASE}/Items/abc").mock(side_effect=httpx.ConnectTimeout("boom"))
    async with JellyfinClient(_settings(JELLYFIN_MAX_RETRIES=0)) as client:
        with pytest.raises(UpstreamTimeout):
            await client.item("abc")


async def test_non_json_success_is_a_contract_error(mock_router: respx.Router) -> None:
    mock_router.get(f"{BASE}/Items/abc").respond(200, text="<html>not json</html>")
    async with JellyfinClient(_settings()) as client:
        with pytest.raises(UpstreamContractError, match="non-JSON"):
            await client.item("abc")


async def test_can_write_metadata_requires_admin(mock_router: respx.Router) -> None:
    mock_router.get(f"{BASE}/Users/Me").respond(
        200, json={"Id": "u", "Name": "sam", "Policy": {"IsAdministrator": False}}
    )
    async with JellyfinClient(_settings()) as client:
        allowed, reason = await client.can_write_metadata()
    assert allowed is False
    assert reason == "key_is_not_elevated"


async def test_can_write_metadata_true_for_admin(mock_router: respx.Router) -> None:
    mock_router.get(f"{BASE}/Users/Me").respond(
        200, json={"Id": "u", "Name": "admin", "Policy": {"IsAdministrator": True}}
    )
    async with JellyfinClient(_settings()) as client:
        allowed, reason = await client.can_write_metadata()
    assert allowed is True
    assert reason is None


async def test_music_libraries_filters_out_non_music(mock_router: respx.Router) -> None:
    mock_router.get(f"{BASE}/Library/MediaFolders").respond(
        200,
        json={
            "Items": [
                {"Name": "Music", "ItemId": "1", "CollectionType": "music"},
                {"Name": "Films", "ItemId": "2", "CollectionType": "movies"},
                {"Name": "Mixed", "ItemId": "3"},
            ]
        },
    )
    async with JellyfinClient(_settings()) as client:
        libraries = await client.music_libraries()
    assert [library.Name for library in libraries] == ["Music"]


async def test_items_by_ids_batches_and_returns_every_item(mock_router: respx.Router) -> None:
    ids = [f"{index:032x}" for index in range(250)]
    mock_router.get(f"{BASE}/Items").mock(
        side_effect=lambda request: httpx.Response(
            200,
            json={
                "Items": [
                    {"Id": value, "Name": value} for value in request.url.params["ids"].split(",")
                ],
                "TotalRecordCount": len(ids),
            },
        )
    )
    async with JellyfinClient(_settings()) as client:
        items = await client.items_by_ids(ids)
    assert len(items) == 250
    assert len(mock_router.calls) == 2, "250 ids must be split across two batched requests"


async def test_items_by_ids_with_no_ids_makes_no_request(mock_router: respx.Router) -> None:
    async with JellyfinClient(_settings()) as client:
        assert await client.items_by_ids([]) == []
    assert not mock_router.calls


async def test_none_query_params_are_dropped(mock_router: respx.Router) -> None:
    route = mock_router.get(f"{BASE}/Items").respond(200, json={"Items": [], "TotalRecordCount": 0})
    async with JellyfinClient(_settings()) as client:
        await client.items(kind="MusicArtist", parent_id=None, search_term=None)
    params = route.calls[0].request.url.params
    assert "parentId" not in params
    assert "searchTerm" not in params
    assert params["includeItemTypes"] == "MusicArtist"


async def test_update_item_posts_the_payload_verbatim(mock_router: respx.Router) -> None:
    route = mock_router.post(f"{BASE}/Items/abc").respond(204)
    payload = {"Name": "Radiohead", "Genres": ["Art Rock"], "Tags": []}
    async with JellyfinClient(_settings()) as client:
        await client.update_item("abc", payload)
    assert route.calls[0].request.content
    import json

    assert json.loads(route.calls[0].request.content) == payload
