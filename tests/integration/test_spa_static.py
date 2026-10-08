"""The built SPA is served from disk, and the fallback must stay inside it.

``spa_fallback`` will serve any file it is asked for, so the containment check
against the resolved bundle root is the only thing between a request and an
arbitrary path on the host. It is load-bearing, not decoration: deleting it makes
``GET /..%2Fsecret.txt`` return a file from outside the bundle, which is what
``test_a_request_cannot_escape_the_bundle`` pins down.

The request is percent-encoded on purpose. A literal ``../`` is collapsed by the
client and by the server before the route ever sees it, so an encoded separator is
what actually delivers ``../secret.txt`` as the path parameter.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from metaedit.config import Settings, get_settings
from metaedit.main import create_app

INDEX = "<html>the built bundle</html>"
ASSET = "console.log('a built asset')"
SECRET = "a file that is not part of the bundle"


@pytest.fixture
def bundle(tmp_path: Path) -> Path:
    """A stand-in for ``apps/web/dist``, sitting next to a file it must not reach."""
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(INDEX)
    (dist / "assets" / "app.js").write_text(ASSET)
    (tmp_path / "secret.txt").write_text(SECRET)
    return dist


@pytest.fixture
def client(bundle: Path) -> Iterator[TestClient]:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        WEB_DIST_DIR=str(bundle),
        JELLYFIN_API_KEY="",
        LASTFM_API_KEY="",
    )
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


def test_a_client_route_falls_back_to_the_bundle(client: TestClient) -> None:
    """A deep link has no file behind it; the SPA router needs index.html."""
    response = client.get("/artists/some-artist/albums")
    assert response.status_code == 200
    assert response.text == INDEX


def test_a_built_asset_is_served(client: TestClient) -> None:
    response = client.get("/assets/app.js")
    assert response.status_code == 200
    assert response.text == ASSET


def test_a_request_cannot_escape_the_bundle(client: TestClient) -> None:
    response = client.get("/..%2Fsecret.txt")
    assert SECRET not in response.text
    assert response.text == INDEX


def test_an_encoded_traversal_through_a_subdirectory_is_refused(client: TestClient) -> None:
    response = client.get("/%2e%2e%2fsecret.txt")
    assert SECRET not in response.text
    assert response.text == INDEX
