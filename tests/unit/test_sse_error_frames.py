"""Error frames must not carry internals.

Streaming endpoints report failures in-band, and that is right: a client that has
already received per-item results should keep them. What such a frame must not do is
hand the browser whatever text an exception happened to produce, because that text
describes *our* internals -- driver names, host and port, filesystem paths, DSN shape.

The regression these tests exist for: ``except Exception: str(exc)`` in both streaming
helpers. It was flagged by CodeQL as ``py/stack-trace-exposure``, and the module
docstring in ``domain/errors.py`` already promised the opposite -- "no upstream payload
ever reaches a client verbatim" -- so this leaked by omission rather than by decision.

A second, quieter bug lived in the same four lines: a caught ``MetaeditError`` that was
not a ``ValidationError``/``NotFoundError`` lost its own code and was reported as
``internal_error``. ``LastfmAuthError`` and ``ArchiveCapReached`` arrive that way, so a
rejected API key was indistinguishable from a crash.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from structlog.testing import capture_logs

from metaedit.api.bulk import _stream as bulk_stream
from metaedit.api.harvest import _stream as harvest_stream
from metaedit.domain.errors import ArchiveCapReached, LastfmAuthError, ValidationError

# Stands in for anything an exception message might drag along: a DSN, a connection
# string, a path. If this reaches a frame, so would the real thing.
INTERNAL = 'connection to server at "127.0.0.1", port 59999 failed'


def frames(body: str) -> list[dict[str, Any]]:
    """Parse an SSE body back into events, so assertions read the payload not the text."""
    return [
        json.loads(line.removeprefix("data: "))
        for line in body.splitlines()
        if line.startswith("data: ")
    ]


async def collect(stream: AsyncIterator[str]) -> str:
    return "".join([chunk async for chunk in stream])


async def failing(exc: BaseException) -> AsyncIterator[dict[str, Any]]:
    yield {"type": "item", "index": 0}
    raise exc


@pytest.mark.parametrize("stream_name", ["bulk", "harvest"])
async def test_unexpected_failure_does_not_disclose_internal_detail(
    stream_name: str,
) -> None:
    events = failing(RuntimeError(f"(psycopg.OperationalError) {INTERNAL}"))
    stream = (
        bulk_stream(events)
        if stream_name == "bulk"
        else harvest_stream(events, reindex_after=False, session=None)  # type: ignore[arg-type]
    )

    body = await collect(stream)

    assert INTERNAL not in body
    assert "psycopg" not in body
    error = next(f for f in frames(body) if f["type"] == "error")
    assert error["message"] != f"(psycopg.OperationalError) {INTERNAL}"


@pytest.mark.parametrize(
    ("stream_name", "expected_code"),
    [("bulk", "internal_error"), ("harvest", "harvest_failed")],
)
async def test_unexpected_failure_keeps_a_reference_and_a_code(
    stream_name: str, expected_code: str
) -> None:
    """The message stays actionable: a reference an operator can find in the log."""
    events = failing(RuntimeError(INTERNAL))
    stream = (
        bulk_stream(events)
        if stream_name == "bulk"
        else harvest_stream(events, reindex_after=False, session=None)  # type: ignore[arg-type]
    )

    body = await collect(stream)

    error = next(f for f in frames(body) if f["type"] == "error")
    assert error["code"] == expected_code
    assert "Reference" in error["message"]


@pytest.mark.parametrize("stream_name", ["bulk", "harvest"])
async def test_internal_detail_is_logged_against_the_shown_reference(
    stream_name: str,
) -> None:
    """Diverting the detail is only defensible if it lands somewhere findable."""
    events = failing(RuntimeError(INTERNAL))
    with capture_logs() as logs:
        stream = (
            bulk_stream(events)
            if stream_name == "bulk"
            else harvest_stream(events, reindex_after=False, session=None)  # type: ignore[arg-type]
        )
        body = await collect(stream)

    error = next(f for f in frames(body) if f["type"] == "error")
    # The reference shown to the client is the one recorded server-side, so an operator
    # can go from a screenshot to the traceback.
    assert any(
        entry.get("reference") and entry["reference"] in error["message"] for entry in logs
    ), logs


@pytest.mark.parametrize("stream_name", ["bulk", "harvest"])
async def test_expected_failure_is_reported_with_its_own_code(stream_name: str) -> None:
    """A curated message is written for a user, so it passes through unchanged."""
    exc = LastfmAuthError("Last.fm rejected the API key", lastfm_code=10)
    stream = (
        bulk_stream(failing(exc))
        if stream_name == "bulk"
        else harvest_stream(failing(exc), reindex_after=False, session=None)  # type: ignore[arg-type]
    )

    error = next(f for f in frames(await collect(stream)) if f["type"] == "error")

    assert error["code"] == "lastfm_key_invalid"
    assert error["message"] == "Last.fm rejected the API key"


async def test_archive_cap_failure_keeps_its_code_rather_than_becoming_internal() -> None:
    """Regression: only Validation/NotFound were passed through, so this became generic."""
    stream = bulk_stream(
        failing(ArchiveCapReached("Archive cap reached", used_bytes=200, cap_bytes=100))
    )

    error = next(f for f in frames(await collect(stream)) if f["type"] == "error")

    assert error["code"] == ArchiveCapReached.code != "internal_error"


async def test_validation_failure_keeps_its_code() -> None:
    stream = harvest_stream(
        failing(ValidationError("kind must be one of artist, album, song")),
        reindex_after=False,
        session=None,  # type: ignore[arg-type]
    )

    error = next(f for f in frames(await collect(stream)) if f["type"] == "error")

    assert error["code"] == ValidationError.code
    assert error["message"] == "kind must be one of artist, album, song"


@pytest.mark.parametrize("stream_name", ["bulk", "harvest"])
async def test_stream_terminates_after_a_failure(stream_name: str) -> None:
    """An in-band error must still close the stream, or a client waits forever."""
    stream = (
        bulk_stream(failing(RuntimeError("boom")))
        if stream_name == "bulk"
        else harvest_stream(failing(RuntimeError("boom")), reindex_after=False, session=None)  # type: ignore[arg-type]
    )

    events = frames(await collect(stream))

    assert events[-1] == {"type": "done"}
    assert [e["type"] for e in events[:2]] == ["item", "error"]
