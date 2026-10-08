"""Server-Sent Events encoding, shared by the streaming endpoints.

The disclosure rule lives in ``domain.errors.public_error_text`` rather than here,
because the per-item result payloads need the same answer. This module is only the
transport: it frames events, and asks the domain what a client may be told about a
failure.
"""

from __future__ import annotations

import json
from typing import Any

from metaedit.domain.errors import MetaeditError, public_error_text


def sse_frame(event: dict[str, Any]) -> str:
    """One SSE frame.

    The event name mirrors the payload's ``type``, so a client can listen by name
    without parsing the body to decide what to do.
    """
    name = str(event.get("type", "message"))
    return f"event: {name}\ndata: {json.dumps(event, default=str)}\n\n"


def error_frame(exc: BaseException, *, code: str) -> dict[str, Any]:
    """Build an in-band error event without disclosing internals.

    ``code`` is the fallback for unexpected failures; an expected failure keeps its own
    code from the error hierarchy.
    """
    return {
        "type": "error",
        "code": exc.code if isinstance(exc, MetaeditError) else code,
        "message": public_error_text(exc),
    }
