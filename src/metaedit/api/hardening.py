"""Hardening middleware: security headers and a request-size cap.

This is a self-hosted tool that people put behind a reverse proxy on their own LAN, so
the defaults should be safe without a checklist. Two things are worth doing in the app
rather than hoping the proxy does them:

* **Security headers.** The SPA is served from the same origin as the API, so a
  successful script injection would run with the ability to call every endpoint --
  including the ones that write to Jellyfin. A restrictive CSP costs nothing here
  because there is no third-party script, no inline script and no remote font.
* **A request-body cap.** The write endpoints take JSON; without a bound, a single
  request can be made arbitrarily large. The cap is on the declared and actual body, so
  a lying ``Content-Length`` does not get a free pass.

Deliberately *not* added: rate limiting of our own API. The expensive operations are
operator-triggered and already bounded (a bulk batch is capped at 500 items and each
write spends one Jellyfin request), and a limiter would mostly get in the way of the
one user this tool has. The Last.fm client does rate-limit, because that is a shared
third-party service with published limits.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

# 2 MiB. A metadata write is a few kilobytes at most; the largest legitimate body is a
# bulk apply carrying an explicit field list, still far under this.
MAX_BODY_BYTES = 2 * 1024 * 1024

# `frame-ancestors 'none'` rather than X-Frame-Options alone: the modern directive is
# what browsers honour, and the legacy header is kept for older ones.
SECURITY_HEADERS: dict[str, str] = {
    "Content-Security-Policy": (
        "default-src 'self'; "
        # Vite emits hashed module scripts and one stylesheet; nothing is inline.
        "script-src 'self'; "
        "style-src 'self'; "
        # Candidate artwork comes from Last.fm's CDN, so those two hosts are allowed
        # for images only.
        "img-src 'self' data: "
        "https://lastfm-img.freetls.fastly.net https://lastfm.freetls.fastly.net; "
        "connect-src 'self'; "
        "font-src 'self'; "
        "object-src 'none'; "
        "base-uri 'none'; "
        "form-action 'self'; "
        "frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    # Nothing here needs a camera, a microphone or a location.
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), interest-cohort=()",
}


def install(app: FastAPI) -> None:
    @app.middleware("http")
    async def harden(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        declared = request.headers.get("content-length")
        if declared is not None:
            try:
                size = int(declared)
            except ValueError:
                return _too_large("Content-Length was not a number")
            if size < 0:
                return _too_large("Content-Length was negative")
            if size > MAX_BODY_BYTES:
                return _too_large(f"body is {size} bytes")

        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            # The SPA's hashed bundle needs no relaxation; if a future endpoint streams
            # something CSP-hostile it should say so explicitly rather than silently
            # weakening this for every response.
            response.headers.setdefault(name, value)
        return response


def _too_large(detail: str) -> JSONResponse:
    return JSONResponse(
        status_code=413,
        content={
            "error": {
                "code": "payload_too_large",
                "message": f"Request body exceeds the {MAX_BODY_BYTES} byte limit.",
                "detail": detail,
                "retryable": False,
            }
        },
    )
