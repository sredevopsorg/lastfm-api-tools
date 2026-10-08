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

    Returns ``(sort_by, sort_order)``. The tuple is the client's shape and Jellyfin
    accepts a comma-separated chain, but **only one key is sent**, and that is a
    deliberate limitation rather than an oversight.

    Paging over a non-total order is unsound: if two items share a sort value, the server
    may order them arbitrarily, and an `OFFSET` window can then return one twice and the
    other never. That is exactly what happened to the archive's derived layer, where
    `ORDER BY name` over 3 duplicate album names lost a row across a 109-item walk.

    A tiebreaker was tried and could not be verified: appending `Id` -- or even a nonsense
    second key -- to `sortBy` produced byte-identical responses on a live 12.2.0 server,
    so there is no evidence the second key is honoured. Adding a key that does nothing
    would be worse than not adding one, because it would read as a guarantee.

    What *was* verified is that the library browse is safe as it stands, and by
    measurement rather than by assumption:

        MusicArtist  639 items  walked 639, distinct 639, no duplicates
        MusicAlbum   502 items  walked 502, distinct 502, no duplicates
        Audio       5442 items  walked 5442, distinct 5442, no duplicates

    and that ten identical requests return one identical ordering, with paged windows
    matching a single-shot fetch of the same range. So Jellyfin's own ordering is stable
    in practice. The guarantee is the server's, not ours; if a future server version
    orders ties differently between requests, paging would silently lose rows and
    `test_the_stub_ignores_an_unknown_sort_key` is the canary that the behaviour is
    modelled at all.
    """
    key = SORT_BY_JELLYFIN_KEY[sort]
    if sort in ORDERLESS_SORT_KEYS:
        return (key,), "Ascending"
    return (key,), "Descending" if order == "desc" else "Ascending"
