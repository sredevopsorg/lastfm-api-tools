"""An error body must survive ``json.dumps``.

An exception handler builds its own response, so ``JSONResponse(content=...)`` runs
plain ``json.dumps`` rather than FastAPI's ``jsonable_encoder``. That made every error
body in the app a latent 500: put a ``datetime`` in a detail and the thing reporting the
failure fails too, leaving the caller with a bare "Internal Server Error" and no idea
what went wrong.

The case that reached users was a song with a ``PremiereDate`` -- songs carry one far
more often than artists or albums, which is why it turned up while editing songs. The
tests below go through the real HTTP handlers rather than calling ``to_jsonable``
directly, because the bug was never in the conversion, it was in *where* the conversion
was missing.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from tests.support.logs import captured_failure_logs

from metaedit.adapters.jellyfin.dto import BaseItemDto
from metaedit.api import errors
from metaedit.api.serialization import to_jsonable
from metaedit.config import Settings, get_settings
from metaedit.domain.errors import ConflictError, MetaeditError, UpstreamError
from metaedit.domain.snapshot import from_dto
from metaedit.main import create_app

TIMESTAMP = datetime(2003, 5, 16, tzinfo=UTC)

# A song exactly as Jellyfin returns it, with the field that caused the failure.
SONG_DTO: dict[str, Any] = {
    "Id": "song-1",
    "Name": "A Song",
    "Type": "Audio",
    "PremiereDate": "2003-05-16T00:00:00.0000000Z",
    "Genres": ["Rock"],
    "Tags": [],
    "LockedFields": [],
    "ProviderIds": {},
    "Studios": [],
    "ExternalUrls": [],
    "People": [],
}


def song_state() -> dict[str, Any]:
    item = from_dto(BaseItemDto.model_validate(SONG_DTO).model_dump(), "Audio")
    return item.as_state()


def _client_raising(exc: Exception) -> TestClient:
    """The real handler stack, with a route that raises ``exc``."""
    app = FastAPI()
    errors.install(app)

    @app.get("/raise")
    def _raise() -> None:
        raise exc

    return TestClient(app, raise_server_exceptions=False)


def test_a_song_state_really_does_carry_a_datetime() -> None:
    """The premise. If this stops holding, the tests below stop testing anything.

    Note it is a *pydantic* model that carries the datetime now, not ``fields``.
    ``fields`` used to hold ``PremiereDate`` as a datetime and no longer does: that
    representation was reaching a JSONB column and an httpx ``json=`` body, and both
    raised ``TypeError`` (see the snapshot tests for the guard). ``to_jsonable`` is
    still required regardless, because a ``BaseItemDto`` really does parse
    ``PremiereDate`` into a datetime -- that is the value an error detail can carry.
    """
    parsed = BaseItemDto.model_validate(SONG_DTO)
    assert isinstance(parsed.PremiereDate, datetime)
    assert not isinstance(song_state()["fields"]["PremiereDate"], datetime)

    # The real end-to-end check: that datetime, in an error detail, survives the
    # handler. `test_a_timestamp_in_an_error_detail_does_not_break_the_error` below
    # covers the same path with a literal; this pins it to the DTO's own value.
    conflict = ConflictError("changed underneath us", current={"premiere": parsed.PremiereDate})
    response = _client_raising(conflict).get("/raise")
    assert response.status_code == 409
    assert response.json()["error"]["current"]["premiere"] == "2003-05-16T00:00:00+00:00"


@pytest.mark.parametrize(
    "detail",
    [
        {"date_last_saved": TIMESTAMP},
        {"nested": {"state": {"fields": {"PremiereDate": TIMESTAMP}}}},
        {"list": [TIMESTAMP, TIMESTAMP]},
        {"when": TIMESTAMP.date()},
        {"enumish": TIMESTAMP},
    ],
    ids=["flat", "nested", "in-list", "date", "datetime"],
)
def test_a_timestamp_in_an_error_detail_does_not_break_the_error(detail: dict[str, Any]) -> None:
    conflict = ConflictError("changed underneath us", current=detail)
    response = _client_raising(conflict).get("/raise")

    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "conflict"
    # The detail survives as text rather than being dropped or stringified wholesale.
    assert "2003-05-16" in json.dumps(body)


def test_the_full_song_state_round_trips_inside_an_error_body() -> None:
    """The exact shape that reached users: the whole state as `current`."""
    conflict = ConflictError("changed underneath us", current={"state": song_state()})
    response = _client_raising(conflict).get("/raise")

    assert response.status_code == 409
    rendered = response.json()["error"]["current"]["state"]["fields"]["PremiereDate"]
    assert rendered == "2003-05-16T00:00:00+00:00"


def test_a_timestamp_in_the_message_and_detail_fields_is_handled() -> None:
    error = UpstreamError("Jellyfin failed", detail={"at": TIMESTAMP})
    response = _client_raising(error).get("/raise")

    assert response.status_code in (500, 502, 503)
    assert response.headers["content-type"].startswith("application/json")


def test_a_value_the_converter_does_not_know_is_still_not_a_crash() -> None:
    """Unknown types must fail loudly, not silently become a string.

    ``to_jsonable`` deliberately passes unrecognised objects through instead of calling
    ``str()`` on them, so a mistake surfaces rather than becoming a mangled value in a
    response body. The app's catch-all still turns it into a reportable 500 -- see
    ``test_unhandled_failures_are_reportable`` for what that body contains.
    """

    class Opaque:
        pass

    app = create_app(Settings(_env_file=None, LOG_JSON=False))  # type: ignore[call-arg]
    app.dependency_overrides[get_settings] = lambda: app.state.settings

    @app.get("/raise-opaque")
    def _raise() -> None:
        raise ConflictError("changed", current={"thing": Opaque()})

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/raise-opaque")

    body = response.json()
    assert response.status_code == 500
    assert body["error"]["code"] == "internal_error"
    assert "Reference" in body["error"]["message"]
    # The type name is what broke, not what a user did -- that is the useful part.
    assert body["error"]["occurred"] == "TypeError"


def test_every_error_body_is_serialisable_once_converted() -> None:
    """A guard against the next subclass adding a non-JSON attribute to `detail`."""
    for error in (
        MetaeditError("plain"),
        ConflictError("conflict", current={"at": TIMESTAMP}),
        UpstreamError("upstream", detail={"at": TIMESTAMP}),
    ):
        json.dumps(to_jsonable(error.to_body()))  # what the handlers actually send


def test_unhandled_failures_are_reportable(caplog) -> None:  # type: ignore[no-untyped-def]
    """A 500 nobody can describe is worse than one with a reference and a type name.

    ``Unexpected server error.`` gave a user nothing to send and nothing to search for.
    The body now carries a reference that appears in the log, the exception type, and the
    path -- enough for a bug report, and no more of our internals than the log already has.
    """
    app = create_app(Settings(_env_file=None, LOG_JSON=False))  # type: ignore[call-arg]
    app.dependency_overrides[get_settings] = lambda: app.state.settings

    @app.get("/always-broken")
    def _raise() -> None:
        raise RuntimeError("something internal about the host")

    with (
        captured_failure_logs(caplog) as logs,
        TestClient(app, raise_server_exceptions=False) as client,
    ):
        response = client.get("/always-broken")

    assert response.status_code == 500
    error = response.json()["error"]
    assert error["code"] == "internal_error"
    assert error["occurred"] == "RuntimeError"
    assert error["path"] == "GET /always-broken"
    assert error["retryable"] is False

    reference = error["reference"]
    assert reference and reference in error["message"]
    # The point of the reference: it is in the log, so a report leads to the traceback.
    assert any(entry.get("reference") == reference for entry in logs), logs
    # And the internal detail is *not* in the response.
    assert "something internal about the host" not in response.text
