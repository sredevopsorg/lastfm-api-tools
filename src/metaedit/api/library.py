"""Library browse and item state.

Read-only: every write lives behind an explicit, diffed apply endpoint so nothing
can change a library by accident.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel

from metaedit.adapters.jellyfin.dto import KIND_LABEL, BaseItemDto, ItemKind
from metaedit.api.deps import JellyfinDep
from metaedit.domain.errors import ValidationError
from metaedit.domain.snapshot import NormalizedItem, from_dto

router = APIRouter(tags=["library"])

KindParam = Literal["artist", "album", "song"]

_KIND_MAP: dict[KindParam, ItemKind] = {
    "artist": "MusicArtist",
    "album": "MusicAlbum",
    "song": "Audio",
}


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
    total: int
    start_index: int


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
    missing_metadata: Annotated[
        bool, Query(description="only items lacking genres, provider ids or an overview")
    ] = False,
) -> ItemSummaryPage:
    item_kind = _KIND_MAP[kind]
    result = await client.items(
        kind=item_kind,
        parent_id=parent_id,
        search_term=search,
        start_index=start_index,
        limit=page_size,
    )
    summaries = [_summary(dto, kind) for dto in result.Items]
    if missing_metadata:
        # Jellyfin has no "missing metadata" filter for music, and a server-side
        # filter would still be a full scan; filter the page we already have.
        summaries = [
            item
            for item in summaries
            if not item.genres or not item.has_provider_ids or not item.has_overview
        ]
    return ItemSummaryPage(
        items=summaries, total=result.TotalRecordCount, start_index=result.StartIndex or 0
    )


@router.get("/items/{item_id}/state")
async def item_state(client: JellyfinDep, item_id: str) -> dict[str, Any]:
    dto = await client.item(item_id)
    kind = kind_of(dto)
    return from_dto(dto.model_dump(), kind).as_state()


@router.post("/items/states")
async def item_states(client: JellyfinDep, item_ids: list[str]) -> list[dict[str, Any]]:
    """Hydrate many items at once -- the multi-select path for bulk editing."""
    dtos = await client.items_by_ids(item_ids)
    states: list[dict[str, Any]] = []
    for dto in dtos:
        kind = kind_of(dto)
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


def kind_of(dto: BaseItemDto) -> ItemKind:
    item_type = dto.Type
    if item_type in ("MusicArtist", "MusicAlbum", "Audio"):
        return item_type  # type: ignore[return-value]
    raise ValidationError(
        f"{item_type!r} is not an editable music item (expected one of {sorted(KIND_LABEL)})."
    )


def normalize(dto: BaseItemDto) -> NormalizedItem:
    """The DTO-to-snapshot mapping, in one place for every caller."""
    return from_dto(dto.model_dump(), kind_of(dto))
