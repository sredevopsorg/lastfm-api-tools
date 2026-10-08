"""Library browse and item state.

Read-only: every write lives behind an explicit, diffed apply endpoint so nothing
can change a library by accident.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel

from metaedit.adapters.jellyfin.dto import BaseItemDto, ItemKind
from metaedit.api.deps import JellyfinDep
from metaedit.domain.browse import (
    DEFAULT_ORDER,
    DEFAULT_SORT,
    SortKey,
    SortOrder,
    jellyfin_sort_by,
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
    # True when a filter could only be applied to the fetched page, which means `total`
    # counts pre-filter rows. The Library UI used to filter client-side while printing
    # the unfiltered total, so the number and the table disagreed with nothing saying so.
    filtered_client_side: bool = False


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
    missing_metadata: Annotated[
        bool,
        Query(
            description=(
                "only items lacking genres, provider ids or an overview. Jellyfin "
                "cannot express this for music, so it filters the fetched page and "
                "`total` stays unfiltered -- see `filtered_client_side`."
            )
        ),
    ] = False,
) -> ItemSummaryPage:
    """Browse one media type, ordered and narrowed.

    `sort` is validated against an allow-list rather than passed through. Live-verified
    on 12.2.0: an unrecognised `sortBy` is accepted and *silently ignored*, so a
    pass-through would let the UI present items in one order while claiming another.
    """
    item_kind = _KIND_MAP[kind]
    sort_by, sort_order = jellyfin_sort_by(sort, order)
    result = await client.items(
        kind=item_kind,
        parent_id=parent_id,
        search_term=search,
        start_index=start_index,
        limit=page_size,
        sort_by=sort_by,
        sort_order=sort_order,
        filters=_server_filters(has_overview=has_overview, year=year),
    )
    summaries = [_summary(dto, kind) for dto in result.Items]
    if missing_metadata:
        # Deliberately a page filter, and `filtered_client_side` says so in the body.
        # Silently narrowing `total` to match would look tidier and be a lie: the number
        # would describe a scan we never performed.
        summaries = [
            item
            for item in summaries
            if not item.genres or not item.has_provider_ids or not item.has_overview
        ]
    return ItemSummaryPage(
        items=summaries,
        total=result.TotalRecordCount,
        start_index=result.StartIndex or 0,
        page_size=page_size,
        sort=sort,
        order=order,
        filtered_client_side=missing_metadata,
    )


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
