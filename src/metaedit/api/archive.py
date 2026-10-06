"""Archive inspection and maintenance endpoints.

The archive is a feature, not an implementation detail: these endpoints expose what
we have accumulated, how close we are to the ToS storage cap, and what the
derivation makes of it.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

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
from metaedit.domain.errors import NotFoundError

router = APIRouter(prefix="/archive", tags=["archive"])

EntityKind = Literal["artist", "album", "track"]

_ENTITY_MODELS = {
    "artist": LastfmArtist,
    "album": LastfmAlbum,
    "track": LastfmTrack,
}


@router.get("/stats")
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


@router.get("/entities")
async def archive_entities(
    session: Annotated[AsyncSession, Depends(get_session)],
    kind: Annotated[EntityKind, Query()] = "artist",
    tag: Annotated[str | None, Query(description="only entities carrying this tag")] = None,
    search: Annotated[str | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    """Search the derived layer offline.

    Reads what the derivation produced and never touches the network: this is what
    answers "what do I already know about X" without spending a request.
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
    rows = (
        (
            await session.execute(
                stmt.order_by(model.name).offset((page - 1) * page_size).limit(page_size)
            )
        )
        .scalars()
        .all()
    )

    return {
        "items": [_entity_summary(kind, row) for row in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/entities/{kind}/{entity_id}")
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

    return {
        **_entity_summary(kind, row),
        "overview": row.overview,
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


@router.get("/tags")
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


@router.get("/aliases")
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
        ]
    }


class ReindexRequest(BaseModel):
    dry_run: bool = Field(
        default=True,
        description="Derive and report without writing. Defaults to true: a rebuild "
        "that writes should be an explicit choice, not an accident.",
    )
    only: Literal["artist", "album", "track", "tag", "similar", "alias"] | None = None


@router.post("/reindex")
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


def _entity_summary(kind: str, row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "identity": row.identity,
        "kind": kind,
        "name": row.name,
        "mbid": row.mbid,
        "url": row.url,
        "listeners": row.listeners,
        "playcount": row.playcount,
        "tags": [entry.get("name") for entry in (row.tags or []) if isinstance(entry, dict)],
        "first_seen_at": row.first_seen_at.isoformat() if row.first_seen_at else None,
        "last_seen_at": row.last_seen_at.isoformat() if row.last_seen_at else None,
        "latest_response_id": row.latest_response_id,
        "extra": _extra_for(kind, row),
    }


def _extra_for(kind: str, row: Any) -> dict[str, Any]:
    """Kind-specific fields, so one response shape covers all three tables."""
    if kind == "artist":
        return {"stats": row.stats, "bio_published": row.bio_published}
    if kind == "album":
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
