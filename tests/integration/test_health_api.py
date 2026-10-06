"""Health endpoints: liveness must not depend on any backing service."""

from __future__ import annotations

from fastapi.testclient import TestClient

from metaedit.config import Settings, get_settings
from metaedit.main import create_app


def _client(**overrides: object) -> TestClient:
    """A client whose endpoints see the test settings, not the process-wide ones.

    The routers inject settings via ``Depends(get_settings)``, which returns a
    process-wide cached instance built from the developer's ``.env``. Without the
    override below, a real credential in ``.env`` leaks into these assertions -- the
    readiness probe reached a live server and reported it healthy, which is how this
    defect surfaced.
    """
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        LOG_JSON=False,
        JELLYFIN_API_KEY="",
        LASTFM_API_KEY="",
        **overrides,  # type: ignore[arg-type]
    )
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    return TestClient(app, raise_server_exceptions=False)


def test_liveness_needs_no_dependencies() -> None:
    # No DATABASE_URL override: the configured default points at a database that
    # may not exist. Liveness must still answer 200.
    with _client() as client:
        response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_request_id_is_echoed() -> None:
    with _client() as client:
        response = client.get("/api/health", headers={"X-Request-ID": "abc123"})
    assert response.headers["X-Request-ID"] == "abc123"


def test_request_id_is_generated_when_absent() -> None:
    with _client() as client:
        response = client.get("/api/health")
    assert len(response.headers["X-Request-ID"]) == 32


def test_openapi_schema_documents_api() -> None:
    with _client() as client:
        schema = client.get("/openapi.json").json()
    assert schema["info"]["title"] == "metaedit"
    assert "/api/health" in schema["paths"]
    assert "/api/health/ready" in schema["paths"]


def test_readiness_reports_degraded_without_dependencies() -> None:
    # Point deliberately at a port nothing listens on, so the result does not
    # depend on whether the developer's Postgres happens to be running.
    with _client(database_url="postgresql+psycopg://nobody@127.0.0.1:1/nothing") as client:
        payload = client.get("/api/health/ready").json()
    assert payload["status"] == "degraded"
    assert payload["checks"]["postgres"]["ok"] is False
    assert payload["checks"]["jellyfin"]["ok"] is False
    assert payload["checks"]["jellyfin"]["error"] == "no_api_key_configured"
    assert payload["checks"]["lastfm"]["configured"] is False


def test_spa_is_not_mounted_without_a_dist_dir() -> None:
    with _client() as client:
        assert client.get("/openapi.json").status_code == 200
        # Nothing should swallow unknown paths when there is no bundle.
        assert client.get("/some/spa/route").status_code == 404
