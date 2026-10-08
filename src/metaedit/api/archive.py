"""Archive inspection and maintenance endpoints.

The archive is a feature, not an implementation detail: these endpoints expose what
we have accumulated, how close we are to the ToS storage cap, and what the
derivation makes of it.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, Body, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from metaedit.api.schemas import (
    ArchiveAliasListResponse,
    ArchiveEntityDetail,
    ArchiveEntityListResponse,
    ArchiveStatsResponse,
    ArchiveTagListResponse,
    ReindexResponse,
)
from metaedit.archive.reindex import (
    ReindexReport,
    derived_summary,
    describe_archive,
    reindex,
)
from metaedit.archive.stats import count_stray_archive_reads, measure, partition_usage
from metaedit.config import Settings, get_settings
from metaedit.db.models import (
    LastfmAlbum,
    LastfmArtist,
    LastfmArtistAlias,
    LastfmEntityTag,
    LastfmSimilarity,
    LastfmTagEdge,
    LastfmTrack,
)
from metaedit.db.session import get_session
from metaedit.domain.archive_sort import (
    DEFAULT_ORDER as ARCHIVE_DEFAULT_ORDER,
)
from metaedit.domain.archive_sort import (
    DEFAULT_SORT as ARCHIVE_DEFAULT_SORT,
)
from metaedit.domain.archive_sort import (
    SortKey as ArchiveSortKey,
)
from metaedit.domain.archive_sort import (
    SortOrder as ArchiveSortOrder,
)
from metaedit.domain.archive_sort import order_by_clauses
from metaedit.domain.errors import NotFoundError

router = APIRouter(prefix="/archive", tags=["archive"])

EntityKind = Literal["artist", "album", "track"]

# The three tables do not share a column set, which is why the per-kind branches
# below exist rather than one uniform attribute read. Reading a column a model does
# not have raises AttributeError at runtime, and the type checker catches it -- which
# is how the original blind `row.overview` read on albums was found.
EntityRow = LastfmArtist | LastfmAlbum | LastfmTrack
EntityModel = type[LastfmArtist] | type[LastfmAlbum] | type[LastfmTrack]

_ENTITY_MODELS: dict[EntityKind, EntityModel] = {
    "artist": LastfmArtist,
    "album": LastfmAlbum,
    "track": LastfmTrack,
}


@router.get("/stats", response_model=ArchiveStatsResponse)
async def archive_stats(
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    stats = await measure(session, settings)
    stats["partitions"] = await partition_usage(session)
    # Surfaced so an operator can see historical rows written before archive hits
    # stopped being logged as requests, rather than silently trusting request_rows.
    stats["stray_archive_reads"] = await count_stray_archive_reads(session)
    stats["derived"] = await derived_summary(session)
    return stats


@router.get("/entities", response_model=ArchiveEntityListResponse)
async def archive_entities(
    session: Annotated[AsyncSession, Depends(get_session)],
    kind: Annotated[EntityKind, Query()] = "artist",
    tag: Annotated[str | None, Query(description="only entities carrying this tag")] = None,
    search: Annotated[str | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
    sort: Annotated[
        ArchiveSortKey, Query(description="field to order by; `id` breaks ties")
    ] = ARCHIVE_DEFAULT_SORT,
    order: Annotated[ArchiveSortOrder, Query(description="direction")] = ARCHIVE_DEFAULT_ORDER,
) -> dict[str, Any]:
    """Search the derived layer offline.

    Reads what the derivation produced and never touches the network: this is what
    answers "what do I already know about X" without spending a request.

    Ordering always ends in a tiebreaker. `ORDER BY name` alone is not a total order --
    8 album rows and 2 track rows share a name with another row, and Postgres may return
    tied rows in a different order per query, so `OFFSET` paging could return one row
    twice and another never. That was not theoretical: paging the live album table one
    row at a time returned 109 rows with 108 distinct, duplicating id 90 and losing id 8.
    """
    model = _ENTITY_MODELS[kind]
    stmt = select(model)

    if search:
        stmt = stmt.where(model.name.ilike(f"%{search}%"))
    if tag:
        # Tag edges reference entities by id, so filter through them.
        subquery = (
            select(LastfmTagEdge.entity_id)
            .where(LastfmTagEdge.entity_kind == kind)
            .where(LastfmTagEdge.tag_name_norm == tag.strip().casefold())
        )
        stmt = stmt.where(model.id.in_(subquery))

    total = int(await session.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    fetched = (
        (
            await session.execute(
                stmt.order_by(*order_by_clauses(model, sort, order))
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        .scalars()
        .all()
    )

    return {
        "items": [_entity_summary(kind, row) for row in _as_entity_rows(fetched)],
        "total": total,
        "page": page,
        "page_size": page_size,
        "sort": sort,
        "order": order,
        # Computed here rather than in the client: a partial last page is where this
        # arithmetic goes wrong, and there is exactly one right answer.
        "pages": max(1, -(-total // page_size)),
    }


@router.get("/entities/{kind}/{entity_id}", response_model=ArchiveEntityDetail)
async def archive_entity(
    session: Annotated[AsyncSession, Depends(get_session)],
    kind: EntityKind,
    entity_id: int,
) -> dict[str, Any]:
    model = _ENTITY_MODELS[kind]
    row = (await session.execute(select(model).where(model.id == entity_id))).scalar_one_or_none()
    if row is None:
        raise NotFoundError(f"No derived {kind} with id {entity_id}")

    tags = (
        (
            await session.execute(
                select(LastfmTagEdge)
                .where(LastfmTagEdge.entity_kind == kind)
                .where(LastfmTagEdge.entity_id == entity_id)
                .order_by(LastfmTagEdge.rank)
            )
        )
        .scalars()
        .all()
    )
    similar: list[dict[str, Any]] = []
    if kind == "artist":
        peers = (
            (
                await session.execute(
                    select(LastfmSimilarity)
                    .where(LastfmSimilarity.artist_id == entity_id)
                    .order_by(LastfmSimilarity.rank)
                )
            )
            .scalars()
            .all()
        )
        similar = [
            {
                "name": peer.peer_name,
                "mbid": peer.peer_mbid,
                "match": float(peer.match) if peer.match is not None else None,
                "rank": peer.rank,
            }
            for peer in peers
        ]

    entity = _as_entity_rows([row])[0]
    return {
        **_entity_summary(kind, entity),
        "tags": [
            {
                "name": edge.tag_name,
                "rank": edge.rank,
                # Null where Last.fm supplied no popularity, never fabricated.
                "count": edge.count,
            }
            for edge in tags
        ],
        "similar": similar,
    }


@router.get("/tags", response_model=ArchiveTagListResponse)
async def archive_tags(
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> dict[str, Any]:
    """Every tag across the archive, with how many distinct entities carry it."""
    rows = (
        (
            await session.execute(
                select(LastfmEntityTag)
                .order_by(LastfmEntityTag.entity_count.desc(), LastfmEntityTag.tag_name_norm)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    total = int(await session.scalar(select(func.count()).select_from(LastfmEntityTag)) or 0)
    return {
        "tags": [
            {"name": row.tag_name, "norm": row.tag_name_norm, "entity_count": row.entity_count}
            for row in rows
        ],
        "total": total,
    }


@router.get("/aliases", response_model=ArchiveAliasListResponse)
async def archive_aliases(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """Last.fm autocorrect corrections, so a known misspelling skips a request."""
    rows = (
        await session.execute(
            select(LastfmArtistAlias, LastfmArtist.name)
            .join(LastfmArtist, LastfmArtist.id == LastfmArtistAlias.canonical_artist_id)
            .order_by(LastfmArtistAlias.requested_name_norm)
        )
    ).all()
    return {
        "aliases": [
            {
                "requested": alias.requested_name_norm,
                "canonical_name": canonical,
                "canonical_artist_id": alias.canonical_artist_id,
            }
            for alias, canonical in rows
        ],
        "total": len(rows),
    }


class ReindexRequest(BaseModel):
    dry_run: bool = Field(
        default=True,
        description="Derive and report without writing. Defaults to true: a rebuild "
        "that writes should be an explicit choice, not an accident.",
    )
    only: Literal["artist", "album", "track", "tag", "similar", "alias"] | None = None


@router.post("/reindex", response_model=ReindexResponse)
async def run_reindex(
    session: Annotated[AsyncSession, Depends(get_session)],
    body: Annotated[ReindexRequest, Body()] = ReindexRequest(),
) -> dict[str, Any]:
    """Rebuild the derived layer from the raw archive.

    ``dry_run`` defaults to **true** here, unlike the CLI: an HTTP client should not
    be able to rebuild the structured layer by accident, and the report is what a UI
    wants in either case.
    """
    report: ReindexReport = await reindex(session, dry_run=body.dry_run, only=body.only)
    if body.dry_run:
        # A dry run must not leave partial state behind.
        await session.rollback()
    else:
        await session.commit()
    return report.as_dict()


@router.get("/diagnose")
async def archive_diagnose(
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """What is archived, and what the derivation cannot use.

    The counterpart to the CLI's ``archive-entities``: a parsing regression appears
    as ``shapes.unexpected`` rising above zero, with the offending methods named.
    """
    return await describe_archive(settings)


def _as_entity_rows(rows: Sequence[Any]) -> list[EntityRow]:
    """Narrow rows whose model was selected by ``kind``.

    SQLAlchemy cannot express "the row type depends on a runtime string" from a
    union of model classes, so the narrowing is stated once here instead of being
    re-derived at every call site.
    """
    return [cast(EntityRow, row) for row in rows]


def _entity_summary(kind: EntityKind, row: EntityRow) -> dict[str, Any]:
    """The fields every derived entity has, in one shape."""
    return {
        "id": row.id,
        "identity": row.identity,
        "kind": kind,
        "name": row.name,
        "mbid": row.mbid,
        "url": row.url,
        "listeners": row.listeners,
        "playcount": row.playcount,
        "overview": overview_of(row),
        "tags": [entry.get("name") for entry in (row.tags or []) if isinstance(entry, dict)],
        "first_seen_at": row.first_seen_at.isoformat() if row.first_seen_at else None,
        "last_seen_at": row.last_seen_at.isoformat() if row.last_seen_at else None,
        "latest_response_id": row.latest_response_id,
        "extra": _extra_for(row),
    }


def overview_of(row: EntityRow) -> str | None:
    """The overview, which every kind now has.

    Albums gained the column after live verification showed album getInfo does return
    a wiki. The previous isinstance narrowing is gone because the type checker now
    guarantees the attribute exists on all three models -- which is a stronger check
    than a runtime branch, and it is the same machinery that caught the original
    blind read on albums.
    """
    return row.overview


def _extra_for(row: EntityRow) -> dict[str, Any]:
    """Kind-specific fields, so one response shape covers all three tables."""
    if isinstance(row, LastfmArtist):
        return {"stats": row.stats, "bio_published": row.bio_published}
    if isinstance(row, LastfmAlbum):
        return {
            "production_year": row.production_year,
            "releasedate": row.releasedate,
            "tracklist": row.tracklist,
        }
    return {
        "duration_ms": row.duration_ms,
        "album_name": row.album_name,
        "album_position": row.album_position,
        "artist_name": row.artist_name,
    }
