"""Domain errors.

One hierarchy, mapped to HTTP in exactly one place (``api.errors``). Nothing
above this layer raises ``httpx`` or ``psycopg`` exceptions, and no upstream
payload ever reaches a client verbatim.
"""

from __future__ import annotations


class MetaeditError(Exception):
    """Base class for every expected failure."""

    code: str = "internal_error"
    http_status: int = 500
    retryable: bool = False

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail

    def to_body(self) -> dict[str, dict[str, object]]:
        body: dict[str, object] = {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }
        if self.detail:
            body["detail"] = self.detail
        return {"error": body}


class UpstreamError(MetaeditError):
    """An upstream service failed in a way we cannot paper over."""

    code = "upstream_error"
    http_status = 502

    def __init__(
        self,
        message: str,
        *,
        upstream_status: int | None = None,
        detail: str | None = None,
    ) -> None:
        super().__init__(message, detail=detail)
        self.upstream_status = upstream_status


class UpstreamAuthError(UpstreamError):
    """Credentials are missing, rejected, or not privileged enough."""

    code = "upstream_auth"
    http_status = 502


class UpstreamUnavailable(UpstreamError):
    code = "upstream_unavailable"
    http_status = 503
    retryable = True


class UpstreamTimeout(UpstreamError):
    code = "upstream_timeout"
    http_status = 504
    retryable = True


class UpstreamContractError(UpstreamError):
    """The upstream returned something we cannot parse.

    The raw body is logged server-side; it is never echoed to the client.
    """

    code = "upstream_contract"
    http_status = 502


class NotFoundError(MetaeditError):
    code = "not_found"
    http_status = 404


class ConflictError(MetaeditError):
    """The item changed underneath us between read and write."""

    code = "conflict"
    http_status = 409

    def __init__(self, message: str, *, current: dict[str, object] | None = None) -> None:
        super().__init__(message)
        self.current = current or {}

    def to_body(self) -> dict[str, dict[str, object]]:
        body = super().to_body()
        body["error"]["current"] = self.current
        return body


class ValidationError(MetaeditError):
    code = "invalid_request"
    http_status = 422


class ArchiveCapReached(MetaeditError):
    """The Last.fm ToS storage cap is reached and a *new* payload was offered.

    ``507 Insufficient Storage`` is the honest status: the request is valid but
    we will not store more Last.fm Data until the operator prunes or raises the
    cap. Repeat observations of already-stored bodies are never blocked.
    """

    code = "archive_cap_reached"
    http_status = 507

    def __init__(self, message: str, *, used_bytes: int, cap_bytes: int) -> None:
        super().__init__(message)
        self.used_bytes = used_bytes
        self.cap_bytes = cap_bytes

    def to_body(self) -> dict[str, dict[str, object]]:
        body = super().to_body()
        body["error"]["used_bytes"] = self.used_bytes
        body["error"]["cap_bytes"] = self.cap_bytes
        body["error"]["remedy"] = (
            "Prune archived history with `metaedit prune-raw --keep-days N --yes`, "
            "or raise ARCHIVE_SOFT_CAP_BYTES if your Last.fm agreement allows it."
        )
        return body


# --------------------------------------------------------------------- Last.fm


class LastfmError(MetaeditError):
    """Base class for Last.fm failures, carrying the upstream error code."""

    code = "lastfm_error"
    http_status = 502

    def __init__(
        self,
        message: str,
        *,
        lastfm_code: int | None = None,
        detail: str | None = None,
    ) -> None:
        super().__init__(message, detail=detail)
        self.lastfm_code = lastfm_code

    def to_body(self) -> dict[str, dict[str, object]]:
        body = super().to_body()
        if self.lastfm_code is not None:
            body["error"]["lastfm_code"] = self.lastfm_code
        return body


class LastfmNotFound(LastfmError):
    """Error 6/7: the resource does not exist on Last.fm.

    Not really an error -- it is a normal outcome that drives candidate fallback,
    and the failing observation is still archived so the absence is dated.
    """

    code = "lastfm_not_found"
    http_status = 404


class LastfmAuthError(LastfmError):
    """Error 10/26: the API key is invalid or suspended. Retrying cannot help."""

    code = "lastfm_key_invalid"
    http_status = 502


class LastfmThrottled(LastfmError):
    """Error 16/29 or HTTP 5xx: retry later."""

    code = "lastfm_throttled"
    http_status = 503
    retryable = True


class LastfmContractError(LastfmError):
    """The response body was not JSON, or not the shape we can store."""

    code = "lastfm_contract"
    http_status = 502
