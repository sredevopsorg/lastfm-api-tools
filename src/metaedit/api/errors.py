"""One place where domain errors become HTTP responses.

Handlers raise ``MetaeditError`` subclasses; nothing above this module knows about
status codes, and no upstream payload is ever echoed to a client.
"""

from __future__ import annotations

from typing import Any

import httpx
import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from metaedit.api.serialization import to_jsonable
from metaedit.domain.errors import MetaeditError

log = structlog.get_logger(__name__)


def install(app: FastAPI) -> None:
    @app.exception_handler(MetaeditError)
    async def metaedit_error_handler(request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, MetaeditError)
        log.warning(
            "request_failed",
            code=exc.code,
            status=exc.http_status,
            message=exc.message,
            detail=exc.detail,
        )
        # A `datetime` in the detail would otherwise make the error body itself
        # unserialisable, and the caller would see a bare 500 instead of this error.
        return JSONResponse(status_code=exc.http_status, content=to_jsonable(exc.to_body()))

    @app.exception_handler(httpx.HTTPError)
    async def httpx_error_handler(request: Request, exc: Exception) -> JSONResponse:
        # An adapter leaked a transport error: a bug, but never a 500 with a stack.
        log.error("unmapped_transport_error", error=type(exc).__name__)
        body: dict[str, Any] = {
            "error": {
                "code": "upstream_error",
                "message": "An upstream request failed unexpectedly.",
                "retryable": True,
            }
        }
        return JSONResponse(status_code=502, content=body)
