"""FastAPI application factory."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

import structlog
from fastapi import APIRouter, FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from metaedit.api import (
    archive,
    bulk,
    errors,
    hardening,
    harvest,
    health,
    info,
    items,
    library,
)

# Aliased: this module uses `settings` as a local name throughout (the Settings instance
# passed to the app), so importing the router module unaliased would shadow it.
from metaedit.api import settings as settings_api
from metaedit.config import Settings, get_settings
from metaedit.db.partitions import ensure_partitions
from metaedit.db.session import dispose_engine, get_session_factory, init_engine
from metaedit.domain.errors import public_failure
from metaedit.logging import configure_logging

log = structlog.get_logger(__name__)

DESCRIPTION = """\
Browse a Jellyfin music library, read artist/album/track metadata from the
Last.fm API, keep a persistent incremental local archive of every Last.fm
response, review a field-level diff, and apply the accepted changes back to
Jellyfin.
"""


def api_router() -> APIRouter:
    router = APIRouter(prefix="/api")
    router.include_router(health.router)
    router.include_router(info.router)
    router.include_router(archive.router)
    router.include_router(library.router)
    router.include_router(items.router)
    router.include_router(bulk.router)
    router.include_router(harvest.router)
    router.include_router(settings_api.router)
    return router


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    init_engine(settings)
    log.info("startup", jellyfin_url=settings.jellyfin_base_url, archive=settings.archive_enabled)

    if settings.archive_enabled:
        # Guard against writes landing in a month with no partition yet.
        try:
            factory = get_session_factory()
            async with factory() as session:
                await ensure_partitions(
                    await session.connection(),
                    months_ahead=settings.archive_partition_months_ahead,
                )
                await session.commit()
        except Exception as exc:
            log.warning("partition_ensure_failed", error=type(exc).__name__)

    try:
        yield
    finally:
        await dispose_engine()
        log.info("shutdown")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(level=settings.log_level, as_json=settings.log_json)

    app = FastAPI(
        title="metaedit",
        description=DESCRIPTION,
        version="0.2.0",
        lifespan=lifespan,
    )
    app.state.settings = settings
    errors.install(app)
    hardening.install(app)
    app.include_router(api_router())

    @app.middleware("http")
    async def request_context(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        structlog.contextvars.bind_contextvars(
            request_id=request_id, method=request.method, path=request.url.path
        )
        try:
            response = await call_next(request)
        finally:
            structlog.contextvars.unbind_contextvars("request_id", "method", "path")
        response.headers["X-Request-ID"] = request_id
        return response

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        """The last resort, and the only place a client sees the reference id.

        The body is deliberately *reportable*: a user who hits this has no other way to
        describe what happened, and "Unexpected server error." gives them nothing to send.
        The reference ties their report to the traceback, the error type names what broke,
        and ``path`` says where -- none of which describes our internals beyond a class
        name, which is the same thing the log line already records.
        """
        failure = public_failure(exc, log_context=f"{request.method} {request.url.path}")
        # `public_failure` already logged the traceback under this reference; this adds
        # where the request was, without a second traceback.
        log.error(
            "unhandled_exception",
            reference=failure["reference"],
            error=type(exc).__name__,
            method=request.method,
            path=request.url.path,
        )
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": failure["code"],
                    "message": failure["message"],
                    "reference": failure["reference"],
                    "occurred": type(exc).__name__,
                    "path": f"{request.method} {request.url.path}",
                    "retryable": False,
                }
            },
        )

    _mount_spa(app, settings)
    return app


def _mount_spa(app: FastAPI, settings: Settings) -> None:
    """Serve the built SPA when present.

    A catch-all route rewrites unknown paths to ``index.html`` so client-side
    routes survive a page refresh, without dragging in a templating dependency.
    The handler is deliberately synchronous: FastAPI runs sync handlers in a
    threadpool, which keeps the event loop free of blocking filesystem calls.
    """
    if not settings.web_dist_dir:
        return
    dist = Path(settings.web_dist_dir).resolve()
    index = dist / "index.html"
    if not index.is_file():
        log.warning("spa_missing", web_dist_dir=str(dist))
        return

    assets = dist / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa_fallback(full_path: str) -> Response:
        if full_path:
            candidate = (dist / full_path).resolve()
            # Containment check: never serve outside the built bundle.
            if candidate.is_file() and candidate.is_relative_to(dist):
                return FileResponse(candidate)
        return FileResponse(index)


app = create_app()
