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
        # A single-item read resolves a user first (Jellyfin 400s without one), and every
        # real server has users. Tests about discovery override this route.
        router.get(f"{BASE}/Users").respond(
            200,
            json=[{"Id": "fixture-user", "Name": "fixture", "Policy": {"IsAdministrator": True}}],
        )
        yield router


async def test_public_probe_sends_identity_but_no_token(
    mock_router: respx.Router,
) -> None:
    """The MediaBrowser scheme carries client identity even with no credential.

    Live-verified: Jellyfin expects the scheme on every request, and a bare token
    without it is parsed as a malformed scheme and rejected with 400. So the header
    is always present -- but with no key configured it must carry no Token parameter.
    """
    route = mock_router.get(f"{BASE}/System/Info/Public").respond(
        200, json={"ServerName": "home", "Version": "10.11.0"}
    )
    async with JellyfinClient(_settings(JELLYFIN_API_KEY="")) as client:
        info = await client.system_info_public()
    assert info.Version == "10.11.0"
    header = route.calls[0].request.headers["Authorization"]
    assert header.startswith("MediaBrowser ")
    assert "Client=" in header and "DeviceId=" in header
    assert "Token=" not in header, "no credential is configured, so none may be sent"


async def test_item_state_requests_every_whitelist_field(mock_router: respx.Router) -> None:
    """A field we do not ask for is a field we cannot round-trip safely."""
    route = mock_router.get(f"{BASE}/Items/abc").respond(200, json={"Id": "abc", "Name": "x"})
    async with JellyfinClient(_settings()) as client:
        await client.item("abc")
    requested = route.calls[0].request.url.params["fields"].split(",")
    for field in ("Genres", "Tags", "ProviderIds", "ExternalUrls", "Overview", "Studios", "People"):
        assert field in requested, f"{field} must be requested so it can be round-tripped"
    assert set(ITEM_FIELDS).issubset(set(requested))


async def test_api_key_is_sent_in_the_mediabrowser_scheme(mock_router: respx.Router) -> None:
    """A bare `<key>` Authorization header is rejected by Jellyfin 12.

    The credential must travel as a quoted `Token=` parameter of the MediaBrowser
    scheme, which is what the official SDKs send.
    """
    route = mock_router.get(f"{BASE}/Users/Me").respond(200, json={"Id": "u", "Name": "admin"})
    async with JellyfinClient(_settings()) as client:
        await client.current_user()
    header = route.calls[0].request.headers["Authorization"]
    assert header.startswith("MediaBrowser ")
    assert 'Token="test-admin-key"' in header


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


async def test_a_bad_request_surfaces_the_reason(
    mock_router: respx.Router,
) -> None:
    """The reason is reported, because a bare status is undiagnosable.

    Withholding the body was the original design and it made every upstream 4xx a dead
    end: the operator saw "rejected the request with 400" and nothing else. The reason is
    now included -- after redaction, which is what makes that safe, and which the test
    below covers.
    """
    mock_router.post(f"{BASE}/Items/abc").respond(
        400, json={"title": "Bad Request", "detail": "PremiereDate was not recognised"}
    )
    async with JellyfinClient(_settings()) as client:
        with pytest.raises(UpstreamContractError) as excinfo:
            await client.update_item("abc", {"Name": "x"})
    assert "PremiereDate was not recognised" in (excinfo.value.detail or "")
    assert "PremiereDate was not recognised" in excinfo.value.message


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


async def test_can_write_metadata_rejects_a_non_admin_user_token(mock_router: respx.Router) -> None:
    """A *user token* that is not an administrator cannot write."""
    mock_router.get(f"{BASE}/Users/Me").respond(
        200, json={"Id": "u", "Name": "sam", "Policy": {"IsAdministrator": False}}
    )
    async with JellyfinClient(_settings()) as client:
        allowed, reason = await client.can_write_metadata()
    assert allowed is False
    assert reason == "user_is_not_an_administrator"


async def test_can_write_metadata_accepts_a_userless_api_key(mock_router: respx.Router) -> None:
    """An API key is userless, so `/Users/Me` answers 400 -- by design.

    Live-verified on 12.2.0: the 400 body is a generic ProblemDetails and does not
    name the reason, so the credential is confirmed by a second probe rather than by
    parsing text that is not there. API keys carry administrator privileges, which is
    exactly what an item update needs.
    """
    mock_router.get(f"{BASE}/Users/Me").respond(
        400,
        json={
            "type": "https://tools.ietf.org/html/rfc9110#section-15.5.1",
            "title": "Bad Request",
            "status": 400,
        },
    )
    mock_router.get(f"{BASE}/System/Info").respond(200, json={"Version": "12.2.0"})
    async with JellyfinClient(_settings()) as client:
        allowed, reason = await client.can_write_metadata()
    assert allowed is True, "an API key is administrator-level"
    assert reason is None


async def test_can_write_metadata_rejects_a_bad_credential(mock_router: respx.Router) -> None:
    mock_router.get(f"{BASE}/Users/Me").respond(401)
    mock_router.get(f"{BASE}/System/Info").respond(401)
    async with JellyfinClient(_settings()) as client:
        allowed, reason = await client.can_write_metadata()
    assert allowed is False
    assert reason == "key_rejected"


async def test_can_write_metadata_true_for_admin(mock_router: respx.Router) -> None:
    mock_router.get(f"{BASE}/Users/Me").respond(
        200, json={"Id": "u", "Name": "admin", "Policy": {"IsAdministrator": True}}
    )
    async with JellyfinClient(_settings()) as client:
        allowed, reason = await client.can_write_metadata()
    assert allowed is True
    assert reason is None


async def test_music_libraries_filters_out_non_music(mock_router: respx.Router) -> None:
    """VirtualFolders, not MediaFolders: live-verified MediaFolders omits ItemId."""
    mock_router.get(f"{BASE}/Library/VirtualFolders").respond(
        200,
        json=[
            {"Name": "Music", "ItemId": "1", "CollectionType": "music"},
            {"Name": "Films", "ItemId": "2", "CollectionType": "movies"},
            {"Name": "Mixed", "ItemId": "3"},
        ],
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


# --------------------------------------------- user-scoped reads (live-verified quirk)


async def test_item_read_resolves_a_user_when_none_is_given(mock_router: respx.Router) -> None:
    """`GET /Items/{id}` returns 400 for a userless API key unless a user is supplied.

    Live-verified on 12.2.0. The contract lists `userId` as optional, so nothing but a
    live call reveals it -- which is how the whole single-item path shipped broken while
    every list-endpoint test passed.
    """
    users = mock_router.get(f"{BASE}/Users").respond(
        200,
        json=[
            {"Id": "zzz-admin", "Name": "second", "Policy": {"IsAdministrator": True}},
            {"Id": "aaa-admin", "Name": "first", "Policy": {"IsAdministrator": True}},
        ],
    )
    item = mock_router.get(f"{BASE}/Items/abc").respond(200, json={"Id": "abc", "Name": "x"})

    async with JellyfinClient(_settings()) as client:
        await client.item("abc")

    assert users.called, "the client must discover a user before reading an item"
    # Sorted by id, so the choice cannot depend on the order the server returns.
    assert item.calls[0].request.url.params["userId"] == "aaa-admin"


async def test_the_resolved_user_is_cached(mock_router: respx.Router) -> None:
    """A library-wide operation must not double its request count resolving a user."""
    users = mock_router.get(f"{BASE}/Users").respond(
        200, json=[{"Id": "u1", "Policy": {"IsAdministrator": True}}]
    )
    mock_router.get(f"{BASE}/Items/abc").respond(200, json={"Id": "abc", "Name": "x"})
    mock_router.get(f"{BASE}/Items/def").respond(200, json={"Id": "def", "Name": "y"})

    async with JellyfinClient(_settings()) as client:
        await client.item("abc")
        await client.item("def")

    assert users.call_count == 1, "the user id is a per-process fact"


async def test_a_configured_user_id_wins(mock_router: respx.Router) -> None:
    """Pinning the user matters on a server where "first" is not the right one."""
    users = mock_router.get(f"{BASE}/Users").respond(
        200, json=[{"Id": "aaa-admin", "Policy": {"IsAdministrator": True}}]
    )
    item = mock_router.get(f"{BASE}/Items/abc").respond(200, json={"Id": "abc", "Name": "x"})

    async with JellyfinClient(_settings(JELLYFIN_USER_ID="pinned-user")) as client:
        await client.item("abc")

    assert not users.called, "a configured id must not trigger discovery"
    assert item.calls[0].request.url.params["userId"] == "pinned-user"


async def test_an_explicit_user_id_overrides_the_resolved_one(mock_router: respx.Router) -> None:
    mock_router.get(f"{BASE}/Users").respond(
        200, json=[{"Id": "aaa-admin", "Policy": {"IsAdministrator": True}}]
    )
    item = mock_router.get(f"{BASE}/Items/abc").respond(200, json={"Id": "abc", "Name": "x"})

    async with JellyfinClient(_settings()) as client:
        await client.item("abc", user_id="caller-choice")

    assert item.calls[0].request.url.params["userId"] == "caller-choice"


async def test_a_non_admin_is_used_when_no_admin_exists(mock_router: respx.Router) -> None:
    """Better a user than no user: any valid id satisfies the endpoint."""
    mock_router.get(f"{BASE}/Users").respond(
        200, json=[{"Id": "plain", "Policy": {"IsAdministrator": False}}]
    )
    item = mock_router.get(f"{BASE}/Items/abc").respond(200, json={"Id": "abc", "Name": "x"})

    async with JellyfinClient(_settings()) as client:
        await client.item("abc")

    assert item.calls[0].request.url.params["userId"] == "plain"


async def test_user_discovery_failure_does_not_mask_the_read(mock_router: respx.Router) -> None:
    """If /Users is unavailable the read proceeds, and its own error is the one reported."""
    mock_router.get(f"{BASE}/Users").respond(500)
    mock_router.get(f"{BASE}/Items/abc").respond(200, json={"Id": "abc", "Name": "x"})

    async with JellyfinClient(_settings()) as client:
        dto = await client.item("abc")

    assert dto.Name == "x"


# ------------------------------------------------------ diagnostics and redaction


async def test_a_rejection_reports_the_reason(mock_router: respx.Router) -> None:
    """A bare status is undiagnosable; Jellyfin's 400s often name the offending field."""
    mock_router.post(f"{BASE}/Items/abc").respond(
        400, json={"title": "Bad Request", "detail": "PremiereDate was not recognised"}
    )
    async with JellyfinClient(_settings()) as client:
        with pytest.raises(UpstreamContractError) as caught:
            await client.update_item("abc", {"Name": "x"})

    assert "PremiereDate was not recognised" in str(caught.value)


async def test_a_rejection_never_echoes_the_credential(mock_router: respx.Router) -> None:
    """Jellyfin does echo the Authorization header in some error bodies.

    Surfacing the reason is only safe because the key is redacted first; forwarding the
    body verbatim would put an administrator credential in a log line and an API response.
    """
    # A 400, because that is the path that carries a body: a 5xx becomes
    # UpstreamUnavailable, which forwards none, so asserting redaction there would be
    # testing something that never happens.
    mock_router.post(f"{BASE}/Items/abc").respond(
        400, json={"Authorization": 'MediaBrowser Token="test-admin-key"', "detail": "bad field"}
    )
    async with JellyfinClient(_settings()) as client:
        with pytest.raises(UpstreamContractError) as caught:
            await client.update_item("abc", {"Name": "x"})

    assert "test-admin-key" not in str(caught.value)
    assert "***" in str(caught.value), "the reason survives, with the key masked"
    assert "bad field" in str(caught.value), "and the diagnostic is still useful"


def test_a_scheme_less_url_is_normalised() -> None:
    """`host:port` is how Jellyfin's own docs write a LAN address.

    Left alone, httpx fails with "Request URL is missing an 'http://' or 'https://'
    protocol", which names neither the setting nor the fix.
    """
    from metaedit.config import Settings

    assert (
        Settings(_env_file=None, JELLYFIN_URL="192.168.1.77:8096").jellyfin_base_url
        == "http://192.168.1.77:8096"
    )
    assert (
        Settings(_env_file=None, JELLYFIN_URL="https://j.example/").jellyfin_base_url
        == "https://j.example"
    )
