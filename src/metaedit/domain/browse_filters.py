"""Which items are worth editing, expressed as filters the server can apply.

The browse screen exists to answer one question -- "what should I fix?" -- and that
question has three *different* kinds of answer, which the API previously conflated into a
single client-side filter:

1. **Server-expressed.** ``has_overview=false`` and ``Years=1995`` are Jellyfin
   parameters that genuinely narrow the result, so ``total`` accounts for them and paging
   is over the filtered set. Cheap and correct.

2. **Derived.** "has no genres", "has no provider ids" -- Jellyfin has no parameter for
   these, and probing confirmed ``Filters=IsMissing`` is accepted and ignored for music.
   Finding them means reading items.

3. **Misleading if treated as (2).** A song with no ``Overview`` is *normal*: measured on
   a live library, 5,441 of 5,442 songs have none, against 254 of 500 artists. Including
   overview in a per-kind "missing" test would mark 99.98% of a song library as
   incomplete and bury the 1,466 songs whose missing genres are actually fixable.

So a browse filter is a named, per-kind definition, and the ones that need a scan say so.
"""

from __future__ import annotations

from typing import Literal, Protocol, runtime_checkable

from metaedit.adapters.jellyfin.dto import ItemKind

# What a filter looks for. Each is checked against the item summary the browse already
# builds, so a scan costs one request per page and no extra parsing.
MissingAspect = Literal["genres", "provider_ids", "overview", "tags"]

# Which aspects are worth reporting as missing, per media type.
#
# `overview` is deliberately absent for songs: almost no song has one, Last.fm track
# wikis are rare, and the editor offers a summary for a track only when the item has
# none. Including it would not be a bug in this module -- it would be a bug in how the
# library screen decides what to show an operator, which is the same thing from the
# operator's seat.
ASPECTS_BY_KIND: dict[ItemKind, frozenset[str]] = {
    "MusicArtist": frozenset({"genres", "provider_ids", "overview", "tags"}),
    "MusicAlbum": frozenset({"genres", "provider_ids", "overview", "tags"}),
    "Audio": frozenset({"genres", "provider_ids", "tags"}),
}

# A scan reads every page of a media type. Songs number in the thousands, so it is
# capped rather than unbounded, and the cap is reported rather than hidden -- a silently
# truncated scan is indistinguishable from a complete one, which is the class of defect
# this whole feature exists to remove.
MAX_SCAN_ITEMS = 2000
SCAN_PAGE_SIZE = 500


def aspects_for(kind: ItemKind) -> frozenset[str]:
    return ASPECTS_BY_KIND[kind]


def normalise_aspects(kind: ItemKind, aspects: frozenset[str]) -> frozenset[str]:
    """Drop aspects that do not apply to this media type.

    Asking a song library for "missing overview" is a reasonable thing for a client to
    do -- the UI may not know the difference -- and the useful answer is "no song is
    reported for that", not a list of 5,441 false positives.
    """
    return aspects & aspects_for(kind)


def is_missing(kind: ItemKind, aspects: frozenset[str], item: SummaryLike) -> bool:
    """Whether an item is missing any of ``aspects``.

    ``any`` rather than ``all``: a filter is a question about what is worth looking at,
    and an item with genres but no provider ids still has something to fix.
    """
    if not aspects:
        return False
    checks = {
        "genres": lambda: not item.genres,
        "tags": lambda: not item.tags,
        "provider_ids": lambda: not item.has_provider_ids,
        "overview": lambda: not item.has_overview,
    }
    return any(checks[aspect]() for aspect in aspects if aspect in checks)


@runtime_checkable
class SummaryLike(Protocol):
    """The slice of an item summary a filter reads.

    Structural, so the API layer's pydantic ``ItemSummary`` satisfies it without being
    imported here and without knowing this module exists. The dependency keeps pointing
    inward: the domain states what it needs, and the boundary happens to provide it.
    """

    genres: list[str]
    tags: list[str]
    has_provider_ids: bool
    has_overview: bool
