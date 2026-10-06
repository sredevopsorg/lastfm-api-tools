"""Canonicalisation: the identity of a request and of a response body.

Two pure functions carry a surprising amount of weight:

* ``canonical_params`` produces the identity used to look a response up in the
  archive. It must be insensitive to things that do not change the answer (key
  order, whitespace, the ``format`` and ``api_key`` plumbing) and sensitive to
  everything that does.
* ``content_id`` produces the content address of a response body: the same bytes
  must always hash to the same key, and different bytes must never collide, or
  the archive would serve the wrong data for an entity.

Canonicalisation happens *before* hashing in both cases, so formatting noise can
never create a spurious duplicate (ADR 0010).
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from typing import Any

# Params that affect only transport or credentials, never the response body.
IGNORED_PARAMS = frozenset({"api_key", "format", "callback", "api_sig", "sk"})


def _normalise_text(value: str) -> str:
    """NFC, collapse internal whitespace, strip. Case is preserved.

    Last.fm treats artist names case-insensitively for matching but echoes back
    the canonical spelling; lowercasing here would make two genuinely different
    requests collide, so the raw case is kept in the request identity.
    """
    return " ".join(unicodedata.normalize("NFC", value).split())


def _normalise_value(value: Any) -> Any:
    if isinstance(value, str):
        return _normalise_text(value)
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return value
    if isinstance(value, list):
        return [_normalise_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _normalise_value(item) for key, item in value.items()}
    return value


def canonical_params(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """The canonical, hashable description of a Last.fm request.

    ``method`` is folded in (and lower-cased, since Last.fm accepts any case) and
    transport-only params are dropped, so ``artist.getinfo`` and ``artist.getInfo``
    with and without ``api_key`` are one identity.
    """
    canonical: dict[str, Any] = {"method": method.strip().lower()}
    for key, value in sorted((params or {}).items()):
        if key.lower() in IGNORED_PARAMS:
            continue
        if value is None or value == "":
            continue
        canonical[key] = _normalise_value(value)
    return canonical


def params_hash(method: str, params: dict[str, Any] | None = None) -> str:
    """Stable hex digest of a request's identity."""
    canonical = canonical_params(method, params)
    blob = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def canonical_body(body: dict[str, Any]) -> str:
    """Canonical JSON text of a response body, for hashing and for storage."""
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_id(body: dict[str, Any]) -> str:
    """Content address of a response body. Identical bodies share one row."""
    return hashlib.sha256(canonical_body(body).encode("utf-8")).hexdigest()


def body_bytes(body: dict[str, Any]) -> int:
    """Bytes attributed to a stored payload, for the ToS cap accounting."""
    return len(canonical_body(body).encode("utf-8"))


def api_key_fingerprint(api_key: str) -> str:
    """A short identifier for an API key, so requests can be correlated.

    Never the key itself: the archive records which key fetched a payload without
    storing a credential.
    """
    if not api_key:
        return ""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]


def is_error_body(body: dict[str, Any]) -> bool:
    """Last.fm reports failures as HTTP 200 with an ``error`` field."""
    return isinstance(body.get("error"), int)
