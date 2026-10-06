"""Hardening: security headers, body-size cap, and secret hygiene.

The secret tests are the ones worth having. This app holds an administrator Jellyfin key
and a Last.fm key, and the SPA is served from the same origin as the API, so a leak could
be as small as one endpoint echoing its settings, or as quiet as a key landing in a log
line. Both are tested rather than assumed.
"""

from __future__ import annotations

import json

import pytest
import respx
from fastapi.testclient import TestClient

from metaedit.api.hardening import MAX_BODY_BYTES, SECURITY_HEADERS
from metaedit.config import Settings, get_settings
from metaedit.main import create_app

BASE = "http://jellyfin.test:8096"
JELLYFIN_KEY = "a" * 32
LASTFM_KEY = "b" * 32


def _settings(**overrides: object) -> Settings:
    fields: dict[str, object] = {
        "LOG_JSON": False,
        "JELLYFIN_URL": BASE,
        "JELLYFIN_API_KEY": JELLYFIN_KEY,
        "LASTFM_API_KEY": LASTFM_KEY,
        "ARCHIVE_ENABLED": True,
    }
    fields.update(overrides)
    return Settings(_env_file=None, **fields)  # type: ignore[arg-type]


def _client(**overrides: object) -> TestClient:
    settings = _settings(**overrides)
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    return TestClient(app, raise_server_exceptions=False)


# ------------------------------------------------------------------- headers


@pytest.mark.parametrize("path", ["/api/health", "/api/info"])
def test_security_headers_are_present(path: str) -> None:
    response = _client().get(path)
    for name, value in SECURITY_HEADERS.items():
        assert response.headers.get(name) == value, name


def test_csp_forbids_framing_and_inline_script() -> None:
    """The SPA shares an origin with the write endpoints, so injected script could write.

    A restrictive policy costs nothing here: no third-party script, no inline script, no
    remote font.
    """
    policy = _client().get("/api/health").headers["Content-Security-Policy"]
    assert "frame-ancestors 'none'" in policy
    assert "script-src 'self'" in policy
    assert "object-src 'none'" in policy
    assert "base-uri 'none'" in policy
    # An `unsafe-inline` or wildcard source would defeat the point of adding this.
    assert "unsafe-inline" not in policy
    assert "unsafe-eval" not in policy
    assert "*" not in policy


def test_headers_are_added_to_error_responses_too() -> None:
    """A 404 is still a response a browser renders, and it must not be frameable."""
    response = _client().get("/api/does-not-exist")
    assert response.status_code == 404
    assert response.headers.get("X-Frame-Options") == "DENY"
    assert response.headers.get("X-Content-Type-Options") == "nosniff"


def test_request_id_is_echoed_for_tracing() -> None:
    response = _client().get("/api/health", headers={"x-request-id": "trace-me"})
    assert response.headers["X-Request-ID"] == "trace-me"


# ------------------------------------------------------------------ body size


def test_an_oversized_declared_body_is_refused() -> None:
    response = _client().post(
        "/api/items/x/diff",
        content=b"{}",
        headers={"Content-Length": str(MAX_BODY_BYTES + 1)},
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"


def test_a_malformed_content_length_is_refused() -> None:
    """A header that is not a number must not become an unbounded read."""
    response = _client().post(
        "/api/items/x/diff", content=b"{}", headers={"Content-Length": "not-a-number"}
    )
    assert response.status_code == 413


def test_a_negative_content_length_is_refused() -> None:
    response = _client().post("/api/items/x/diff", content=b"{}", headers={"Content-Length": "-1"})
    assert response.status_code == 413


def test_a_normal_body_is_accepted() -> None:
    """The cap must not be so eager that ordinary requests fail."""
    response = _client().post("/api/archive/reindex", json={"dry_run": True})
    assert response.status_code in (200, 500), "reaching the handler is what matters here"


# --------------------------------------------------------------------- secrets


def test_no_endpoint_echoes_a_credential() -> None:
    """The SPA reads these endpoints, so none of them may return a key."""
    client = _client()
    for path in ("/api/health", "/api/health/ready", "/api/info", "/api/libraries"):
        raw = client.get(path).text
        assert JELLYFIN_KEY not in raw, path
        assert LASTFM_KEY not in raw, path


@respx.mock
def test_an_upstream_failure_does_not_echo_the_key() -> None:
    """A 4xx from Jellyfin must not be forwarded verbatim.

    Jellyfin echoes the request in some error bodies, which would put our credential in
    a response the browser can read.
    """
    respx.get(f"{BASE}/System/Info/Public").respond(200, json={"ServerName": "x"})
    respx.get(f"{BASE}/Items/abc").respond(
        500, json={"Authorization": f'MediaBrowser Token="{JELLYFIN_KEY}"', "detail": "boom"}
    )
    response = _client().get("/api/items/abc/state")
    assert JELLYFIN_KEY not in response.text


def test_the_openapi_document_contains_no_secret() -> None:
    """A generated client is fetched from this, so it is public by definition."""
    raw = _client().get("/openapi.json").text
    assert JELLYFIN_KEY not in raw
    assert LASTFM_KEY not in raw
    # The schema mentions the field names, which is fine, but not their values.
    assert "jellyfin_url" in raw or "api_root" in raw


def test_settings_do_not_serialise_a_credential() -> None:
    """`model_dump` is the obvious way a secret reaches a log line or a JSON response.

    The keys are `SecretStr`, so a dump yields an opaque marker and `repr` is clean. The
    accessor methods are the only way to obtain the value, which makes every use of it an
    explicit, greppable decision rather than an accident of attribute access.
    """
    settings = _settings()
    dumped = json.dumps(settings.model_dump(), default=str)

    assert JELLYFIN_KEY not in dumped
    assert LASTFM_KEY not in dumped
    assert JELLYFIN_KEY not in repr(settings)
    assert LASTFM_KEY not in repr(settings)
    assert "**********" in dumped, "SecretStr should render its masked marker"

    # And the deliberate accessors still work, or the app could not authenticate at all.
    assert settings.jellyfin_key() == JELLYFIN_KEY
    assert settings.lastfm_key() == LASTFM_KEY


def test_an_error_detail_never_carries_a_header() -> None:
    """Our own errors must not echo the request that failed.

    The upstream body is echoed only through a sanitised detail field, so a credential
    that travelled in a header cannot reappear in a response the browser can read.
    """
    from metaedit.domain.errors import UpstreamError

    error = UpstreamError("Jellyfin refused the request", detail="http_401")
    body = json.dumps(error.to_body())
    assert error.detail == "http_401"
    assert "Token" not in body
    assert "Authorization" not in body
