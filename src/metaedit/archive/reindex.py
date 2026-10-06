"""Rebuild the derived archive tables from the raw archive.

The raw layer (``lastfm_request`` + ``lastfm_response``) is the source of truth.
Everything derived -- entity dimensions, tag edges, similarity, autocorrect
aliases -- is a pure function of it, so changing the parsing models or the tag
split must never require re-fetching from Last.fm.

**Implement this against ``docs/design/0003-derivation-and-reindex.md``**, which
fixes the identity rules, the field-selection rules, the staging swap and the
acceptance criteria. Two of its requirements are easy to violate accidentally:

* no wall-clock in a derived column (everything comes from raw ``requested_at``),
* an entity that gains an MBID later must resolve to one row, not two.

This module is deliberately unimplemented until phase 3 is scheduled: raising is
better than a half-working derivation that silently corrupts the derived layer.
"""

from __future__ import annotations

from typing import Any

from metaedit.config import Settings

DESIGN_DOC = "docs/design/0003-derivation-and-reindex.md"


async def reindex(
    settings: Settings,
    *,
    dry_run: bool = False,
    since: str | None = None,
    only: str | None = None,
) -> dict[str, Any]:
    """Re-derive archive tables. ``dry_run`` reports the delta without writing.

    See the design document for the full contract; ``metaedit archive-stats``
    reports the raw layer in the meantime.
    """
    raise NotImplementedError(
        f"archive reindex lands in phase 3; see {DESIGN_DOC} for the contract"
    )
