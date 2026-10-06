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

from metaedit.domain.diff import SelectionError
from metaedit.domain.errors import MetaeditError, ValidationError

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
        return JSONResponse(status_code=exc.http_status, content=exc.to_body())

    @app.exception_handler(SelectionError)
    async def selection_error_handler(request: Request, exc: Exception) -> JSONResponse:
        # Raised by the diff/apply layer when a caller names a field the plan does not
        # permit, or one Jellyfin has locked. That is the caller's mistake, so it is a
        # 422 with a reason -- not a 500, which is what an unmapped ValueError became.
        assert isinstance(exc, SelectionError)
        error = ValidationError(str(exc))
        log.warning("invalid_selection", message=str(exc))
        return JSONResponse(status_code=error.http_status, content=error.to_body())

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
