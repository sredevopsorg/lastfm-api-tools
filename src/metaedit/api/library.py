"""Library browse and item state.

Read-only: every write lives behind an explicit, diffed apply endpoint so nothing
can change a library by accident.
"""

from __future__ import annotations

import os
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel

from metaedit.adapters.jellyfin.client import JellyfinClient
from metaedit.adapters.jellyfin.dto import BaseItemDto, ItemKind
from metaedit.api.deps import JellyfinDep
from metaedit.domain.browse import (
    DEFAULT_ORDER,
    DEFAULT_SORT,
    SortKey,
    SortOrder,
    jellyfin_sort_by,
)
from metaedit.domain.browse_filters import (
    MAX_SCAN_ITEMS,
    SCAN_PAGE_SIZE,
    MissingAspect,
    is_missing,
    normalise_aspects,
)
from metaedit.domain.snapshot import NormalizedItem, from_dto
from metaedit.service.planning import ITEM_KIND_BY_QUERY, item_kind_for

router = APIRouter(tags=["library"])

# The browse vocabulary, spelled once in the service layer so this endpoint, the bulk
# selection and the harvest selection cannot disagree about what a "song" is. Written
# out as a Literal rather than left as `str` so the generated SPA types still enumerate
# it -- a `str` here turns every route's `kind` into an untyped string, which is how the
# library page's `limit`/`page_size` mistake became possible in the first place.
KindParam = Literal["artist", "album", "song"]

_KIND_MAP: dict[str, ItemKind] = ITEM_KIND_BY_QUERY


class ItemSummary(BaseModel):
    id: str
    kind: KindParam
    name: str
    album: str | None = None
    album_artist: str | None = None
    year: int | None = None
    genres: list[str] = []
    tags: list[str] = []
    has_provider_ids: bool = False
    has_overview: bool = False
    child_count: int | None = None


class ItemSummaryPage(BaseModel):
    items: list[ItemSummary]
    # The count of everything matching the query, not of `items`. A page is a window,
    # and a client that renders `total` as "how many are in this table" is wrong in a
    # way it cannot detect -- so `sort` and `order` are echoed back for the same reason:
    # the UI should show what the server *did*, not what it asked for.
    total: int
    start_index: int
    page_size: int
    sort: SortKey
    order: SortOrder
    # What the scan covered, when the filter needed one.
    scan: ScanInfo | None = None


class ScanInfo(BaseModel):
    """What a filter that required reading items actually read.

    A filtered count is only meaningful next to the number it was drawn from. Without
    this, "38 items missing genres" is indistinguishable from "38 in the first 200",
    and silence about the difference is how the previous implementation managed to print
    an unfiltered total above a filtered table for as long as it did.
    """

    scanned: int
    matched: int
    # The scan stopped at the cap, so `matched` is a lower bound and `total` is `matched`
    # over an incomplete set. The UI must say so rather than present either as complete.
    truncated: bool
    limit: int


class LibraryInfo(BaseModel):
    id: str | None
    name: str
    locations: list[str] = []


def _summary(dto: BaseItemDto, kind: KindParam) -> ItemSummary:
    album_artists = dto.album_artist_names()
    return ItemSummary(
        id=str(dto.Id),
        kind=kind,
        name=dto.Name or "",
        album=dto.Album,
        album_artist=album_artists[0] if album_artists else None,
        year=dto.ProductionYear,
        genres=list(dto.Genres or []),
        tags=list(dto.Tags or []),
        has_provider_ids=bool(dto.ProviderIds),
        has_overview=bool((dto.Overview or "").strip()),
        child_count=dto.ChildCount,
    )


@router.get("/libraries")
async def libraries(client: JellyfinDep) -> list[LibraryInfo]:
    folders = await client.music_libraries()
    return [
        LibraryInfo(
            id=folder.ItemId, name=folder.Name or "", locations=list(folder.Locations or [])
        )
        for folder in folders
    ]


@router.get("/items")
async def items(
    client: JellyfinDep,
    kind: Annotated[KindParam, Query()] = "artist",
    parent_id: Annotated[str | None, Query()] = None,
    search: Annotated[str | None, Query()] = None,
    start_index: Annotated[int, Query(ge=0)] = 0,
    page_size: Annotated[int, Query(ge=1, le=500)] = 100,
    sort: Annotated[SortKey, Query(description="field to order by")] = DEFAULT_SORT,
    order: Annotated[SortOrder, Query(description="direction; ignored by `random`")] = (
        DEFAULT_ORDER
    ),
    has_overview: Annotated[
        bool | None,
        Query(
            description=(
                "Jellyfin's own filter: true returns only items with an overview, "
                "false only those without. Applied by the server, so `total` reflects it."
            )
        ),
    ] = None,
    year: Annotated[
        int | None,
        Query(
            ge=1000,
            le=9999,
            description="Jellyfin's own filter: exact production year.",
        ),
    ] = None,
    missing: Annotated[
        list[MissingAspect] | None,
        Query(
            description=(
                "only items lacking any of these: genres, provider_ids, overview, tags. "
                "Jellyfin cannot express this for music, so it requires a scan and the "
                "response reports what the scan covered under `scan`. Aspects that do "
                "not apply to the media type are ignored rather than matching everything."
            )
        ),
    ] = None,
) -> ItemSummaryPage:
    """Browse one media type, ordered and narrowed.

    `sort` is validated against an allow-list rather than passed through. Live-verified
    on 12.2.0: an unrecognised `sortBy` is accepted and *silently ignored*, so a
    pass-through would let the UI present items in one order while claiming another.
    """
    item_kind = _KIND_MAP[kind]
    sort_by, sort_order = jellyfin_sort_by(sort, order)
    filters = _server_filters(has_overview=has_overview, year=year)

    aspects = normalise_aspects(item_kind, frozenset(missing or ()))
    if not aspects:
        result = await client.items(
            kind=item_kind,
            parent_id=parent_id,
            search_term=search,
            start_index=start_index,
            limit=page_size,
            sort_by=sort_by,
            sort_order=sort_order,
            filters=filters,
        )
        return ItemSummaryPage(
            items=[_summary(dto, kind) for dto in result.Items],
            total=result.TotalRecordCount,
            start_index=result.StartIndex or 0,
            page_size=page_size,
            sort=sort,
            order=order,
        )

    page, scan = await _scan_for_missing(
        client,
        kind=kind,
        item_kind=item_kind,
        aspects=aspects,
        parent_id=parent_id,
        search=search,
        start_index=start_index,
        page_size=page_size,
        sort_by=sort_by,
        sort_order=sort_order,
        filters=filters,
    )
    return ItemSummaryPage(
        items=page.items,
        total=page.total,
        start_index=page.start_index,
        page_size=page_size,
        sort=sort,
        order=order,
        scan=scan,
    )


async def _scan_for_missing(
    client: JellyfinClient,
    *,
    kind: KindParam,
    item_kind: ItemKind,
    aspects: frozenset[str],
    parent_id: str | None,
    search: str | None,
    start_index: int,
    page_size: int,
    sort_by: tuple[str, ...],
    sort_order: str,
    filters: dict[str, str],
) -> tuple[ItemSummaryPage, ScanInfo]:
    """Read matching items, so the page and the count describe the *filtered* set.

    This is the honest version of what the browse screen used to do in the browser: it
    filtered the one page it had fetched, and the header above it printed the
    unfiltered total, so the two disagreed and nothing said so.

    The scan reads in pages and stops at ``MAX_SCAN_ITEMS``. When it stops early the
    result is a lower bound and ``ScanInfo.truncated`` says so -- silently returning a
    short list would be the same defect in a new place.
    """
    matched: list[ItemSummary] = []
    scanned = 0
    probe = 0
    truncated = True
    cap = _scan_cap()
    while probe < cap:
        batch = await client.items(
            kind=item_kind,
            parent_id=parent_id,
            search_term=search,
            start_index=probe,
            limit=min(SCAN_PAGE_SIZE, cap - probe),
            sort_by=sort_by,
            sort_order=sort_order,
            filters=filters,
        )
        if not batch.Items:
            truncated = False
            break
        for dto in batch.Items:
            summary = _summary(dto, kind)
            scanned += 1
            if is_missing(item_kind, aspects, summary):
                matched.append(summary)
        probe += len(batch.Items)
        if probe >= (batch.TotalRecordCount or 0):
            truncated = False
            break

    # `truncated` still True here means the loop exited on the cap, which is the only
    # other way out. The `if` above clears it only when we genuinely reached the end of
    # the result set -- with a small cap the two can coincide, and reading the loop
    # condition as "we stopped because the cap" was wrong the first time this was
    # written: a cap of 1 over a 1-item result reported a complete scan.
    window = matched[start_index : start_index + page_size]
    return (
        ItemSummaryPage(
            items=window,
            total=len(matched),
            start_index=start_index,
            page_size=page_size,
            sort=DEFAULT_SORT,
            order=DEFAULT_ORDER,
        ),
        ScanInfo(scanned=scanned, matched=len(matched), truncated=truncated, limit=cap),
    )


def _scan_cap() -> int:
    """How many items a filter scan may read.

    Overridable by an environment variable so the truncation path has a test. The
    alternative is a fixture with 2,000+ items purely to reach the limit, which would
    make the suite slower to prove something a smaller fixture can prove exactly.

    Read per call rather than cached, so a test can change it via settings and get a
    deterministic result -- a cached cap would make the test order-dependent.
    """
    raw = os.environ.get("METAEDIT_BROWSE_SCAN_CAP")
    if raw and raw.isdigit() and int(raw) > 0:
        return int(raw)
    return MAX_SCAN_ITEMS


def _server_filters(*, has_overview: bool | None, year: int | None) -> dict[str, str]:
    """Translate browse filters into Jellyfin's query parameters.

    Only parameters live-verified to actually narrow a result belong here. ``Filters``
    (``IsMissing``/``IsNotMissing``) is accepted by the server and changes nothing for
    music libraries, so it is deliberately not used -- a filter that silently does
    nothing is worse than one that is absent.
    """
    filters: dict[str, str] = {}
    if has_overview is not None:
        filters["hasOverview"] = "true" if has_overview else "false"
    if year is not None:
        filters["Years"] = str(year)
    return filters


@router.get("/items/{item_id}/state")
async def item_state(client: JellyfinDep, item_id: str) -> dict[str, Any]:
    dto = await client.item(item_id)
    kind = item_kind_for(dto)
    return from_dto(dto.model_dump(), kind).as_state()


@router.post("/items/states")
async def item_states(client: JellyfinDep, item_ids: list[str]) -> list[dict[str, Any]]:
    """Hydrate many items at once -- the multi-select path for bulk editing."""
    dtos = await client.items_by_ids(item_ids)
    states: list[dict[str, Any]] = []
    for dto in dtos:
        kind = item_kind_for(dto)
        states.append(from_dto(dto.model_dump(), kind).as_state())
    return states


@router.post("/items/{item_id}/refresh")
async def refresh_item(client: JellyfinDep, item_id: str) -> dict[str, str]:
    """Ask Jellyfin to re-run *its* metadata providers for one item.

    Deliberately separate from applying our edits: this is Jellyfin's pipeline,
    not ours, and it can overwrite values we just wrote.
    """
    await client.refresh_item(item_id, metadata_refresh_mode="ValidationOnly")
    return {"status": "queued"}


def normalize(dto: BaseItemDto) -> NormalizedItem:
    """The DTO-to-snapshot mapping, delegating the media-type rule to the service layer."""
    return from_dto(dto.model_dump(), item_kind_for(dto))
