"""The /api/info endpoint.

This endpoint had no test, and that absence is why a real bug survived: it sent its own
Jellyfin request with a bare ``Authorization: <key>`` header and treated any non-200
from ``/Users/Me`` as "not elevated". Once the client was corrected to the MediaBrowser
scheme, ``/api/info`` went on reporting a perfectly good API key as unusable -- and the
UI reads this endpoint to decide whether to offer the editing features at all.

These tests drive the credential families the endpoint has to distinguish.
"""

from __future__ import annotations

import httpx
import respx
from fastapi.testclient import TestClient

from metaedit.config import Settings, get_settings
from metaedit.main import create_app

BASE = "http://jellyfin.test:8096"


def _settings(**overrides: object) -> Settings:
    fields: dict[str, object] = {
        "LOG_JSON": False,
        "JELLYFIN_URL": BASE,
        "JELLYFIN_API_KEY": "a" * 32,
        "LASTFM_API_KEY": "b" * 32,
        "ARCHIVE_ENABLED": True,
    }
    fields.update(overrides)
    return Settings(_env_file=None, **fields)  # type: ignore[arg-type]


def _client(settings: Settings) -> TestClient:
    app = create_app(settings)
    # The routers read settings through the dependency, not from the app object.
    app.dependency_overrides[get_settings] = lambda: settings
    return TestClient(app, raise_server_exceptions=False)


@respx.mock
def test_info_reports_a_userless_api_key_as_elevated() -> None:
    """An API key is userless, so /Users/Me answers 400 by design.

    That 400 is a *success* signal: an API key carries administrator privileges, which
    is what an item update requires. Treating it as failure made every API-key
    deployment look unable to write.
    """
    respx.get(f"{BASE}/System/Info/Public").respond(
        200, json={"ServerName": "home", "Version": "12.2.0"}
    )
    respx.get(f"{BASE}/Users/Me").respond(
        400,
        json={
            "type": "https://tools.ietf.org/html/rfc9110#section-15.5.1",
            "title": "Bad Request",
            "status": 400,
        },
    )
    respx.get(f"{BASE}/System/Info").respond(200, json={"Version": "12.2.0"})

    body = _client(_settings()).get("/api/info").json()
    assert body["jellyfin"]["reachable"] is True
    assert body["jellyfin"]["elevated"] is True, "an API key is administrator-level"
    assert body["jellyfin"]["error"] is None
    assert body["jellyfin"]["version"] == "12.2.0"


@respx.mock
def test_info_reports_a_non_admin_user_token_as_not_elevated() -> None:
    """The other credential family: a user token that is not an administrator."""
    respx.get(f"{BASE}/System/Info/Public").respond(
        200, json={"ServerName": "home", "Version": "12.2.0"}
    )
    respx.get(f"{BASE}/Users/Me").respond(
        200, json={"Id": "u", "Name": "sam", "Policy": {"IsAdministrator": False}}
    )
    respx.get(f"{BASE}/System/Info").respond(200, json={"Version": "12.2.0"})

    body = _client(_settings()).get("/api/info").json()
    assert body["jellyfin"]["elevated"] is False
    assert body["jellyfin"]["error"] == "user_is_not_an_administrator"


@respx.mock
def test_info_sends_the_mediabrowser_scheme() -> None:
    """The regression that made this endpoint lie.

    A bare ``Authorization: <key>`` is rejected by Jellyfin 12, so the endpoint's own
    request failed regardless of the key's validity. Delegating to the client is what
    keeps them from disagreeing.
    """
    respx.get(f"{BASE}/System/Info/Public").respond(200, json={"ServerName": "home"})
    me = respx.get(f"{BASE}/Users/Me").respond(400, json={"title": "Bad Request"})
    respx.get(f"{BASE}/System/Info").respond(200, json={"Version": "12.2.0"})

    _client(_settings()).get("/api/info")
    header = me.calls[0].request.headers["Authorization"]
    assert header.startswith("MediaBrowser "), header
    assert 'Token="' in header


@respx.mock
def test_info_reports_a_rejected_key_as_not_elevated() -> None:
    respx.get(f"{BASE}/System/Info/Public").respond(200, json={"ServerName": "home"})
    respx.get(f"{BASE}/Users/Me").respond(401)
    respx.get(f"{BASE}/System/Info").respond(401)

    body = _client(_settings()).get("/api/info").json()
    assert body["jellyfin"]["elevated"] is False
    assert body["jellyfin"]["error"] == "key_rejected"


def test_info_without_a_key_says_so() -> None:
    body = _client(_settings(JELLYFIN_API_KEY="")).get("/api/info").json()
    assert body["jellyfin"]["key_configured"] is False
    assert body["jellyfin"]["error"] == "no_api_key_configured"


def test_info_reports_lastfm_and_the_archive_cap() -> None:
    body = _client(_settings(ARCHIVE_ENABLED=False)).get("/api/info").json()
    assert body["lastfm"]["configured"] is True
    assert body["archive"]["enabled"] is False
    assert body["archive"]["cap_bytes"] > 0


@respx.mock
def test_info_never_exposes_a_credential() -> None:
    """The browser reads this endpoint, so it must not echo either key."""
    respx.get(f"{BASE}/System/Info/Public").respond(200, json={"ServerName": "home"})
    respx.get(f"{BASE}/Users/Me").respond(400, json={"title": "Bad Request"})
    respx.get(f"{BASE}/System/Info").respond(200, json={"Version": "12.2.0"})

    raw = _client(_settings()).get("/api/info").text
    assert "a" * 32 not in raw
    assert "b" * 32 not in raw


@respx.mock
def test_info_survives_an_unreachable_server() -> None:
    respx.get(f"{BASE}/System/Info/Public").mock(side_effect=httpx.ConnectError("refused"))
    body = _client(_settings()).get("/api/info").json()
    assert body["jellyfin"]["reachable"] is False
    assert body["jellyfin"]["error"]
    assert body["lastfm"]["configured"] is True, "one service being down hides nothing else"
