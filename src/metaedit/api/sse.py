"""Server-Sent Events encoding, shared by the streaming endpoints.

One rule lives here, and it is the reason this is a module rather than a duplicated
helper: **an error frame carries text we wrote, never text an exception happened to
produce.** ``MetaeditError`` is the curated hierarchy -- every message in it was
written to be read by a user -- so those pass through unchanged. Anything else is a
bug or an infrastructure fault, and its ``str()`` describes our internals: driver
names, host and port, filesystem paths. That belongs in the log, where an operator
can join it to the reference id the client is shown.

This mirrors the rule the adapters already follow (``adapters/jellyfin/client.py``
sanitises upstream bodies before they reach a message). The stream layer simply had
no equivalent, so a catch-all leaked by omission rather than by intent.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import structlog

from metaedit.domain.errors import MetaeditError

log = structlog.get_logger(__name__)


def sse_frame(event: dict[str, Any]) -> str:
    """One SSE frame.

    The event name mirrors the payload's ``type``, so a client can listen by name
    without parsing the body to decide what to do.
    """
    name = str(event.get("type", "message"))
    return f"event: {name}\ndata: {json.dumps(event, default=str)}\n\n"


def error_frame(exc: BaseException, *, code: str) -> dict[str, Any]:
    """Build an in-band error event without disclosing internals.

    ``code`` is the fallback used only for unexpected failures; an expected failure
    keeps its own code from the error hierarchy.
    """
    if isinstance(exc, MetaeditError):
        return {"type": "error", "code": exc.code, "message": exc.message}

    reference = uuid.uuid4().hex[:12]
    log.error("stream_failed", reference=reference, error_code=code, exc_info=exc)
    return {
        "type": "error",
        "code": code,
        "message": (
            f"Internal error. Reference {reference} -- see the server log for the traceback."
        ),
    }
