"""The browse query: how a list of library items is ordered and narrowed.

A browse request is four independent things -- which items, in what order, which way,
and how much of it -- and this module names them so that the API, the SPA and the
Jellyfin adapter cannot disagree about what a parameter means.

The mapping in ``SORT_BY_JELLYFIN_KEY`` is an **allow-list**, not a convenience. Live
verification against Jellyfin 12.2.0 showed that an unrecognised ``sortBy`` value is
*accepted and silently ignored*, falling back to the default order. A UI that offered a
sort the server does not implement would then display items in one order while claiming
another, and nothing would report a problem. So an unknown key is refused here, at the
boundary, where it can be a 422 instead of a lie.
"""

from __future__ import annotations

from typing import Literal

SortKey = Literal["name", "sort_name", "date_added", "year", "random"]
SortOrder = Literal["asc", "desc"]

# Browse vocabulary -> Jellyfin's `sortBy` value.
#
# Every entry was verified to change the returned order on a live 12.2.0 server. Keys
# that Jellyfin accepts but ignores (or that only look plausible) are deliberately
# absent: leaving one in would reintroduce the silent-fallback problem this list exists
# to prevent.
SORT_BY_JELLYFIN_KEY: dict[str, str] = {
    # `SortName` is Jellyfin's own sort field: it honours ForcedSortName, so "The Cure"
    # files under C. `Name` is the displayed name as-is, which is a different question
    # and worth being able to ask separately.
    "name": "Name",
    "sort_name": "SortName",
    "date_added": "DateCreated",
    "year": "ProductionYear",
    "random": "Random",
}

# `Random` is a shuffle: asking for it descending is meaningless, and Jellyfin is free
# to ignore the order. Recorded here rather than silently passed through.
ORDERLESS_SORT_KEYS = frozenset({"random"})

DEFAULT_SORT: SortKey = "sort_name"
DEFAULT_ORDER: SortOrder = "asc"


def jellyfin_sort_by(sort: str, order: str) -> tuple[tuple[str, ...], str]:
    """Translate a browse sort into the parameters the Jellyfin client takes.

    Returns ``(sort_by, sort_order)`` -- a tuple because the client's ``sort_by`` is a
    sequence and Jellyfin permits a tiebreaker chain. Only one key is used today; the
    shape is the client's, not ours, and keeping it avoids a second translation when a
    tiebreaker is wanted.
    """
    key = SORT_BY_JELLYFIN_KEY[sort]
    if sort in ORDERLESS_SORT_KEYS:
        return (key,), "Ascending"
    return (key,), "Descending" if order == "desc" else "Ascending"
