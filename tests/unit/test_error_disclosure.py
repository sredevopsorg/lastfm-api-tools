"""One definition of what a client may be told about a failure.

CodeQL reported the same leak twice -- first the SSE catch-all, then the per-item failure
payloads -- because each place answered "is this exception safe to quote?" for itself.
``public_error_text`` exists so the question is answered once, and these tests are what
hold that answer still.

The distinction the policy draws is not severity, it is *provenance*: a ``MetaeditError``
message was written by us for a reader, so it passes through. Anything else -- an ORM
error, a transport error, a plain bug -- has a ``str()`` that describes our internals.
"""

from __future__ import annotations

import pytest
from tests.support.logs import captured_failure_logs

from metaedit.domain.diff import SelectionError
from metaedit.domain.errors import (
    ArchiveCapReached,
    LastfmAuthError,
    MetaeditError,
    NotFoundError,
    UpstreamUnavailable,
    ValidationError,
    public_error_text,
)

# Stands in for anything an unexpected exception's text might drag along.
INTERNAL = 'connection to server at "127.0.0.1", port 59999 failed'


@pytest.mark.parametrize(
    "exc",
    [
        ValidationError("kind must be one of artist, album, song"),
        NotFoundError("no such item"),
        SelectionError("Genres is locked by another client"),
        LastfmAuthError("Last.fm rejected the API key", lastfm_code=10),
        UpstreamUnavailable("Jellyfin is unreachable"),
        ArchiveCapReached("Archive cap reached", used_bytes=200, cap_bytes=100),
    ],
)
def test_a_curated_message_is_passed_through(exc: MetaeditError) -> None:
    assert public_error_text(exc) == exc.message


def test_an_unexpected_failure_is_reduced_to_a_reference() -> None:
    text = public_error_text(RuntimeError(f"(psycopg.OperationalError) {INTERNAL}"))

    assert INTERNAL not in text
    assert "psycopg" not in text
    assert "Reference" in text


def test_the_detail_is_logged_under_the_reference_the_user_is_shown(caplog) -> None:  # type: ignore[no-untyped-def]
    """Diverting the detail is only defensible if it stays findable."""
    with captured_failure_logs(caplog) as logs:
        text = public_error_text(RuntimeError(INTERNAL))

    assert any(entry.get("reference") and entry["reference"] in text for entry in logs), logs
    assert any(entry.get("exc_info") for entry in logs), "the traceback must be recorded"


def test_each_failure_gets_its_own_reference() -> None:
    """A shared id would make two unrelated faults look like one."""
    first = public_error_text(RuntimeError("a"))
    second = public_error_text(RuntimeError("b"))

    assert first != second


def test_a_curated_failure_is_not_logged_as_a_fault(caplog) -> None:  # type: ignore[no-untyped-def]
    """An expected failure is a normal outcome; logging it as a fault would be noise."""
    with captured_failure_logs(caplog) as logs:
        public_error_text(NotFoundError("no such item"))

    assert logs == []


def test_selection_error_is_an_expected_failure_not_a_bare_value_error() -> None:
    """It was a ``ValueError``, which left its HTTP status in another layer's handler.

    Two hierarchies also meant the write path could not catch it and ``MetaeditError``
    together, so the per-item failure payload had to special-case it.
    """
    assert issubclass(SelectionError, MetaeditError)
    assert SelectionError.code == "invalid_request"
    assert SelectionError.http_status == 422


def test_selection_error_keeps_a_usable_str() -> None:
    """``pytest.raises(match=...)`` and the diff tests read the message this way."""
    exc = SelectionError("Genres is not proposed for this item")

    assert str(exc) == "Genres is not proposed for this item"
    assert exc.message == str(exc)
