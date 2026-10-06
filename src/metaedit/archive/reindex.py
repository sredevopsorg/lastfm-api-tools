"""Rebuild the derived archive tables from the raw archive (phase 3).

The raw layer (``lastfm_request`` + ``lastfm_response``) is the source of truth.
Everything derived -- entity dimensions, tag edges, similarity, autocorrect
aliases -- is a pure function of it, so changing the parsing models or the tag
split must never require re-fetching from Last.fm.

This module is filled in during phase 3; the CLI already depends on its shape.
"""

from __future__ import annotations

from typing import Any

from metaedit.config import Settings


async def reindex(
    settings: Settings,
    *,
    dry_run: bool = False,
    since: str | None = None,
    only: str | None = None,
) -> dict[str, Any]:
    """Re-derive archive tables. ``dry_run`` reports the delta without writing."""
    raise NotImplementedError(
        "archive reindex lands in phase 3; run `metaedit archive-stats` meanwhile"
    )
