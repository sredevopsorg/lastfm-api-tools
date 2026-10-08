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

from collections.abc import Mapping, Sequence
from typing import Literal

from metaedit.domain.errors import ValidationError

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

# Facet -> Jellyfin's query parameter. Both take a comma-separated list and **union** it;
# live-verified on 12.2.0 that a list of two artists returns the sum of each alone
# (ABBA=2, a-ha=1, both=3). The same parameter repeated unions too, which is why the API
# joins rather than sending it several times: one spelling is one thing to get wrong.
FACET_PARAMS: dict[str, str] = {"artist_ids": "ArtistIds", "album_ids": "AlbumIds"}

# Which facets can narrow which media type.
#
# Not merely "the ones that make sense": Jellyfin *applies* the wrong pair and returns
# **zero**. ``ArtistIds`` on ``IncludeItemTypes=MusicArtist`` returns 0 of 639, because no
# artist item's own credit list contains its own id, and ``AlbumIds`` on a music album does
# the same. An empty table that looks like missing data is the exact failure this project
# treats as a defect, so the wrong pair is refused at the boundary instead of answered with
# a zero that reads as a fact.
FACET_KINDS: dict[str, frozenset[str]] = {
    "artist_ids": frozenset({"album", "song"}),
    "album_ids": frozenset({"song"}),
}


def facet_filters(
    kind: str,
    *,
    artist_ids: Sequence[str] = (),
    album_ids: Sequence[str] = (),
) -> dict[str, str]:
    """Jellyfin's facet parameters for one media type, refused where they cannot apply.

    Ids are expected to have been shape-checked already (``domain.identifiers``): this
    function decides *whether* a facet applies to a media type, not whether an id is one.
    """
    requested: Mapping[str, Sequence[str]] = {
        "artist_ids": artist_ids,
        "album_ids": album_ids,
    }
    filters: dict[str, str] = {}
    for facet, values in requested.items():
        if not values:
            continue
        allowed = FACET_KINDS[facet]
        if kind not in allowed:
            readable = " or ".join(sorted(allowed))
            raise ValidationError(
                f"{facet} does not narrow {kind}s: Jellyfin applies it and returns zero, "
                f"which reads as an empty library rather than a filter that cannot work. "
                f"It applies to {readable} only."
            )
        filters[FACET_PARAMS[facet]] = ",".join(values)
    return filters


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
