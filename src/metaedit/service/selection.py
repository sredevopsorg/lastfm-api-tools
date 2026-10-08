"""The narrowing a browse query, a batch diff, a removal and a harvest all share.

There are two kinds of narrowing in this application and they cost different things:

* **Forwarded.** ``artist_ids``/``album_ids`` are Jellyfin's own parameters, so the answer
  arrives filtered and the server's own total is the truth.
* **Read.** ``missing`` (which Jellyfin cannot express for music) and exclusion patterns
  (which it cannot express at all) mean the items have to be read and tested here.

Both eventually need the same three decisions -- which ids are safe to send, which fields a
pattern sees, and whether an item to hand is one the operator asked for -- and this module
is where those live so the four callers cannot answer them differently. The browse screen
uses the same three functions; it just windows the result instead of collecting a
signature.

The reason this is not merely tidiness: a selection on every one of these paths leads to a
*Jellyfin write*. Two readings of "excluded" would mean a batch writes to an item the
operator had filtered out on screen, and nothing would report it.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Self

from metaedit.adapters.jellyfin.client import JellyfinClient
from metaedit.adapters.jellyfin.dto import ItemKind
from metaedit.domain.browse import facet_filters
from metaedit.domain.browse_filters import (
    MAX_SCAN_ITEMS,
    SCAN_PAGE_SIZE,
    SummaryLike,
    is_missing,
    normalise_aspects,
)
from metaedit.domain.errors import ValidationError
from metaedit.domain.exclusion import compile_patterns, exclusion_reason, is_excluded
from metaedit.domain.identifiers import clean_item_ids
from metaedit.service.labels import labels_from_dto
from metaedit.service.planning import ITEM_KIND_BY_QUERY


@dataclass(frozen=True, slots=True)
class _Aspects:
    """A Jellyfin DTO presented in the shape the shared predicate reads.

    A small adapter rather than teaching the domain about ``BaseItemDto``: the filter is
    shared with the library browse, which feeds it item *summaries*, and those are a
    different type with the same four fields. Narrowing here keeps the comparison in one
    place -- two implementations of "has no genres" is how the browse and the bulk editor
    would come to disagree about which items need work.
    """

    genres: list[str]
    tags: list[str]
    has_provider_ids: bool
    has_overview: bool


def aspects_of(dto: Any) -> SummaryLike:
    """Read a Jellyfin DTO as the filter's four fields."""
    return _Aspects(
        genres=list(dto.Genres or []),
        tags=list(dto.Tags or []),
        has_provider_ids=bool(dto.ProviderIds),
        has_overview=bool((dto.Overview or "").strip()),
    )


@dataclass(frozen=True, slots=True)
class Selection:
    """A validated narrowing: which items, and why.

    Built through :meth:`build` rather than directly, because every field needs a decision
    that must not be skipped -- ids shape-checked, patterns compiled, aspects normalised
    against the media type -- and a dataclass anyone can construct is a dataclass anyone
    can construct wrong.
    """

    kind: str
    item_kind: ItemKind
    artist_ids: tuple[str, ...] = ()
    album_ids: tuple[str, ...] = ()
    patterns: tuple[str, ...] = ()
    aspects: frozenset[str] = frozenset()

    @classmethod
    def build(
        cls,
        *,
        kind: str,
        artist_ids: Iterable[str] | None = None,
        album_ids: Iterable[str] | None = None,
        exclude: Iterable[str] | None = None,
        missing: Iterable[str] | None = None,
    ) -> Self:
        item_kind = ITEM_KIND_BY_QUERY.get(kind)
        if item_kind is None:
            raise ValidationError(
                f"unknown selection kind {kind!r}; expected one of {sorted(ITEM_KIND_BY_QUERY)}"
            )
        return cls(
            kind=kind,
            item_kind=item_kind,
            # Shape-checked before anything is sent: Jellyfin discards an unparseable list
            # in full and answers with the unfiltered library, which would widen a
            # selection into a library-wide batch.
            artist_ids=tuple(clean_item_ids(artist_ids, field="artist_ids")),
            album_ids=tuple(clean_item_ids(album_ids, field="album_ids")),
            patterns=compile_patterns(exclude),
            # Normalised, so asking a song library for "missing overview" selects nothing
            # rather than every song -- a song without an overview is the normal state.
            aspects=normalise_aspects(item_kind, frozenset(missing or ())),
        )

    @property
    def needs_read(self) -> bool:
        """Whether answering this means reading items rather than asking Jellyfin.

        Mirrors the browse screen's condition exactly, and for the same reason: when
        nothing has to be tested locally, one request answers the selection and a scan
        would multiply it for no gain.
        """
        return bool(self.aspects or self.patterns)

    def jellyfin_params(self) -> dict[str, str]:
        """The facet parameters, refused here if they cannot apply to this media type."""
        return facet_filters(self.kind, artist_ids=self.artist_ids, album_ids=self.album_ids)

    def excluded_by_pattern(self, labels: Sequence[str]) -> bool:
        return bool(self.patterns) and is_excluded(labels, self.patterns)

    def excluded_reason(self, labels: Sequence[str]) -> str | None:
        if not self.patterns:
            return None
        return exclusion_reason(labels, self.patterns)

    def selected_by_aspects(self, dto: Any) -> bool:
        """Whether the item satisfies the requested aspects.

        ``True`` when none were requested -- "no aspect filter" selects everything, which
        is not the same statement as ``is_missing`` returning False for everything. Reading
        that function as the keep test made an exclusion-only scan match nothing at all.
        """
        if not self.aspects:
            return True
        return is_missing(self.item_kind, self.aspects, aspects_of(dto))

    def carries(self, dto: Any) -> bool:
        """Whether this item is one the operator asked for overall."""
        if self.excluded_by_pattern(labels_from_dto(dto)):
            return False
        return self.selected_by_aspects(dto)


@dataclass(slots=True)
class SelectionOutcome:
    """What a selection read, and what it cost.

    The counts are not decoration. ``limit: 25`` with an exclusion pattern can legitimately
    return four items, and without saying so the operator sees a short batch and has no way
    to tell it from a broken filter. That is the same "quietly too short" defect the browse
    screen's scan reporting exists to remove.
    """

    items: list[Any] = field(default_factory=list)
    # How many items were read. `None` when the request was forwarded to Jellyfin and
    # nothing was read here -- zero would claim a read that found nothing to consider,
    # which is a different statement.
    scanned: int | None = None
    excluded: int = 0
    # The scan stopped at the cap before either filling the selection or reaching the end
    # of the library, so this outcome is not the whole answer.
    truncated: bool = False
    # Deliberately no `__bool__`/`__len__`. One was here, meaning "found some items", and
    # it made `if report:` -- the obvious way to test a report is present -- take the
    # "absent" branch for a selection that read items and excluded all of them. The
    # counters then read as zero, so a batch that had dropped 20 items reported dropping
    # none. A report's emptiness and its existence are different questions; ask for
    # `.items` when the first is meant.


async def collect(
    client: JellyfinClient,
    *,
    selection: Selection,
    parent_id: str | None = None,
    search: str | None = None,
    ids: Iterable[str] | None = None,
    limit: int,
    scan_cap: int = MAX_SCAN_ITEMS,
) -> SelectionOutcome:
    """The items this selection names, reading the library only when it has to.

    Three paths, and the difference between them is what the caller can honestly report:

    * **Explicit ids.** The caller named the items, so nothing is over-fetched: exclusion
      is applied and *counted*, but a short result is the caller's own doing and the count
      is the only thing that explains it.
    * **Forwarded.** No aspect and no pattern, so one request answers it and ``scanned``
      stays ``None``.
    * **Read.** Pages through the media type until ``limit`` items have passed both tests,
      the library ends, or ``scan_cap`` is reached -- the last of which sets ``truncated``.

    Over-fetching rather than filtering a full page is what keeps ``limit`` meaningful: a
    batch of 25 that dropped 21 items to a pattern would otherwise return four, and the
    operator would have to guess why.
    """
    explicit = [item_id for item_id in (ids or []) if item_id]
    if explicit:
        candidates = list(await client.items_by_ids(explicit[:limit]))
        kept = [dto for dto in candidates if selection.carries(dto)]
        return SelectionOutcome(
            items=kept,
            scanned=len(candidates),
            excluded=len(candidates) - len(kept),
        )

    params = selection.jellyfin_params()
    if not selection.needs_read:
        page = await client.items(
            kind=selection.item_kind,
            parent_id=parent_id,
            search_term=search,
            limit=limit,
            filters=params,
        )
        return SelectionOutcome(items=list(page.Items))

    return await _read(
        client,
        selection=selection,
        parent_id=parent_id,
        search=search,
        limit=limit,
        filters=params,
        cap=scan_cap,
    )


async def _read(
    client: JellyfinClient,
    *,
    selection: Selection,
    parent_id: str | None,
    search: str | None,
    limit: int,
    filters: dict[str, str],
    cap: int,
) -> SelectionOutcome:
    """Page through the media type, testing each item, until the selection is full."""
    kept: list[Any] = []
    scanned = 0
    excluded = 0
    # Left True so that the *only* way out with it still set is exhausting the cap. Each
    # other exit clears it explicitly, because reading the loop condition as "we stopped
    # because of the cap" is how a complete scan once reported itself as truncated (and,
    # with a cap that coincided with the end of the result set, the reverse).
    truncated = True
    probe = 0
    while probe < cap and len(kept) < limit:
        batch = await client.items(
            kind=selection.item_kind,
            parent_id=parent_id,
            search_term=search,
            start_index=probe,
            limit=min(SCAN_PAGE_SIZE, cap - probe),
            filters=filters,
        )
        if not batch.Items:
            truncated = False
            break
        for dto in batch.Items:
            scanned += 1
            labels = labels_from_dto(dto)
            if selection.excluded_by_pattern(labels):
                excluded += 1
                continue
            if not selection.selected_by_aspects(dto):
                continue
            kept.append(dto)
            if len(kept) >= limit:
                break
        probe += len(batch.Items)
        if len(kept) >= limit:
            # The selection is full. More matching items exist than were asked for, and
            # that is the requested limit being honoured -- not an incomplete read.
            truncated = False
            break
        if probe >= (batch.TotalRecordCount or 0):
            truncated = False
            break

    return SelectionOutcome(
        items=kept[:limit],
        scanned=scanned,
        excluded=excluded,
        truncated=truncated,
    )


__all__ = [
    "Selection",
    "SelectionOutcome",
    "aspects_of",
    "collect",
]
