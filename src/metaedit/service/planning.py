"""Building a reviewable plan for one Jellyfin item.

Lives in the service layer because both the single-item endpoints and bulk editing
need it, and because it is orchestration: it reads the item, resolves an archived
candidate, scores the match and maps the changes. Keeping it out of ``api/`` means the
HTTP layer stays a thin translation of requests into service calls.
"""

from __future__ import annotations

from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from metaedit.adapters.jellyfin.client import JellyfinClient
from metaedit.archive.candidates import candidate_from_entity
from metaedit.archive.resolve import (
    ResolvedCandidate,
    candidate_identity,
    match_context,
    resolve_candidates,
)
from metaedit.db.models import LastfmAlbum, LastfmArtist, LastfmTrack
from metaedit.domain.confidence import score
from metaedit.domain.diff import DiffPlan, build_plan
from metaedit.domain.errors import NotFoundError, ValidationError
from metaedit.domain.mapping import Mode, build_changes
from metaedit.domain.snapshot import NormalizedItem, from_dto
from metaedit.domain.tags import TagPolicy
from metaedit.domain.writable import ItemKind

# Entity kind (archive vocabulary) per Jellyfin media type.
ENTITY_KIND_BY_ITEM_KIND: dict[ItemKind, str] = {
    "MusicArtist": "artist",
    "MusicAlbum": "album",
    "Audio": "track",
}

# Browse vocabulary -> Jellyfin media type.
#
# "song", not "track", because that is the word every *query surface* uses: the library
# browse's `kind` parameter, the bulk and harvest selection bodies, and the SPA's labels.
# The derived archive layer calls the same entity "track" (`lastfm_track`, `ReindexKind`),
# which is a different surface with a different reader -- someone writing SQL.
#
# The two vocabularies are separate on purpose and that separation is what broke: the bulk
# selection took its vocabulary from the *archive* side (`SelectionKind` said "song") and
# its lookup from the *archive* map (`ITEM_KIND_BY_QUERY` said "track"), so every bulk diff
# over songs was rejected by the service with "unknown selection kind 'song'". Defined here
# so the query mappings cannot drift apart again.
QUERY_KIND_BY_ITEM_KIND: dict[ItemKind, str] = {
    "MusicArtist": "artist",
    "MusicAlbum": "album",
    "Audio": "song",
}

QUERY_KINDS: tuple[str, ...] = tuple(QUERY_KIND_BY_ITEM_KIND.values())

ITEM_KIND_BY_QUERY: dict[str, ItemKind] = {
    query: item_kind for item_kind, query in QUERY_KIND_BY_ITEM_KIND.items()
}

MODELS_BY_ENTITY_KIND: dict[str, Any] = {
    "artist": LastfmArtist,
    "album": LastfmAlbum,
    "track": LastfmTrack,
}


def item_kind_for(dto: Any) -> ItemKind:
    """The media type of a DTO, refusing anything that is not editable music."""
    item_type = getattr(dto, "Type", None)
    if item_type in ENTITY_KIND_BY_ITEM_KIND:
        return cast(ItemKind, item_type)
    raise ValidationError(
        f"{item_type!r} is not an editable music item "
        f"(expected one of {sorted(ENTITY_KIND_BY_ITEM_KIND)})"
    )


def normalise(dto: Any) -> NormalizedItem:
    return from_dto(dto.model_dump(), item_kind_for(dto))


async def entity_by_id(
    session: AsyncSession, item: NormalizedItem, entity_id: int, entity_kind: str
) -> ResolvedCandidate | None:
    """Look up one archived entity and score it against the item.

    A caller naming an entity id may be choosing something the name-based candidate
    list did not surface, which is normal: an MBID match yields exactly one candidate,
    and a deliberate choice should not have to appear in a list first.
    """
    model = MODELS_BY_ENTITY_KIND.get(entity_kind)
    if model is None:
        raise ValidationError(
            f"unknown entity kind {entity_kind!r}; expected one of {sorted(MODELS_BY_ENTITY_KIND)}"
        )
    id_column: Any = model.id
    row = (await session.execute(select(model).where(id_column == entity_id))).scalar_one_or_none()
    if row is None:
        return None
    return ResolvedCandidate(
        entity=row,
        kind=item.kind,
        confidence=score(match_context(item), candidate_identity(row)),
        from_mbid=False,
    )


async def build_plan_for_item(
    *,
    session: AsyncSession,
    item: NormalizedItem,
    entity_id: int,
    entity_kind: str | None = None,
    overrides: dict[str, Mode] | None = None,
    tag_policy: TagPolicy | None = None,
) -> DiffPlan:
    """Assemble a plan from an already-read item and a chosen archived entity."""
    kind = entity_kind or ENTITY_KIND_BY_ITEM_KIND[item.kind]
    chosen = await entity_by_id(session, item, entity_id, kind)
    if chosen is None:
        raise NotFoundError(f"Archived {kind} {entity_id} does not exist")
    return plan_from_candidate(item=item, chosen=chosen, overrides=overrides, tag_policy=tag_policy)


def plan_from_candidate(
    *,
    item: NormalizedItem,
    chosen: ResolvedCandidate,
    overrides: dict[str, Mode] | None = None,
    tag_policy: TagPolicy | None = None,
) -> DiffPlan:
    candidate = candidate_from_entity(chosen.entity, kind=chosen.entity_kind)
    mapping = build_changes(
        item,
        candidate,
        tag_policy=tag_policy or TagPolicy(),
        overrides=overrides or {},
        # Nothing is pre-selected unless the match is trustworthy enough to act on
        # without a human reading it.
        default_selected=chosen.confidence.accepts_by_default,
    )
    return build_plan(
        item=item,
        candidate=candidate,
        confidence=chosen.confidence,
        mapping=mapping,
        locked_fields=item.get("LockedFields") or [],
    )


async def build_best_plan(
    *,
    session: AsyncSession,
    client: JellyfinClient,
    dto: Any,
    entity_kind: str | None = None,
    overrides: dict[str, Mode] | None = None,
    tag_policy: TagPolicy | None = None,
) -> DiffPlan:
    """Read an item and plan against its best archived candidate.

    Used by bulk editing, where the operator is not choosing per item. Raises when no
    archived candidate matches, so the caller can record the item as skipped rather
    than writing something arbitrary.
    """
    item = normalise(dto)
    resolved = await resolve_candidates(session, item, limit=1)
    if not resolved:
        raise NotFoundError(
            f"no archived Last.fm candidate matches {item.name!r}; fetch it first, then diff again"
        )
    return plan_from_candidate(
        item=item, chosen=resolved[0], overrides=overrides, tag_policy=tag_policy
    )
