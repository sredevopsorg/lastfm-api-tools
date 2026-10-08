"""The edit flow: resolve candidates, preview a diff, apply, undo.

The ordering of these endpoints is the safety model. A caller cannot reach a write
without first seeing a diff, and a diff cannot be assembled without naming a specific
candidate, so "apply whatever Last.fm says" is not expressible.

Reading is separated from writing at the HTTP level too: ``/diff`` is a POST only
because a policy override is a body, and it writes nothing.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends, Header, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from metaedit.api.deps import JellyfinDep
from metaedit.api.schemas import (
    ApplyResponse,
    CandidatesResponse,
    DiffResponse,
    SnapshotListResponse,
)
from metaedit.archive.resolve import describe_candidate, resolve_candidates
from metaedit.config import Settings, get_settings
from metaedit.db.models import Snapshot
from metaedit.db.session import get_session
from metaedit.domain.errors import NotFoundError, ValidationError
from metaedit.domain.tags import TagPolicy
from metaedit.service import genre_blacklist
from metaedit.service.apply import ApplyOutcome, apply_plan, revert_snapshot
from metaedit.service.planning import build_plan_for_item, normalise

router = APIRouter(tags=["editing"])

ModeOverride = Literal["keep_existing", "fill_if_empty", "replace", "merge"]


class CandidatesRequest(BaseModel):
    limit: int = Field(default=5, ge=1, le=25)
    # Fetching from Last.fm when the archive has nothing is a separate, explicit act:
    # it spends a rate-limited request and needs the API key.
    refresh: bool = Field(
        default=False,
        description="Reserved: fetching from Last.fm is not part of the read-only diff path.",
    )


class FieldPolicyOverride(BaseModel):
    """Per-field mode for one request, so a single edit need not change configuration."""

    field: str
    mode: ModeOverride


class TagPolicyRequest(BaseModel):
    genre_limit: int = Field(default=5, ge=0, le=50)
    style_limit: int = Field(default=10, ge=0, le=50)
    min_count: int = Field(default=0, ge=0)
    extra_blacklist: list[str] = Field(default_factory=list)


class DiffRequest(BaseModel):
    entity_id: int = Field(description="id of the archived entity to map from")
    entity_kind: Literal["artist", "album", "track"] | None = Field(
        default=None, description="defaults to the kind implied by the Jellyfin item"
    )
    overrides: list[FieldPolicyOverride] = Field(default_factory=list)
    tag_policy: TagPolicyRequest | None = None


class ApplyRequest(DiffRequest):
    # None means "use the plan's default selection", which is empty for anything
    # needing review.
    fields: list[str] | None = Field(
        default=None, description="fields to write; omit to use the plan's default selection"
    )
    expected_etag: str | None = Field(
        default=None,
        description="the etag the diff was prepared against; a mismatch is refused",
    )
    confirm: bool = Field(
        default=False,
        description="must be true: a write is never the default outcome of a request",
    )


@router.post("/items/{item_id}/candidates", response_model=CandidatesResponse)
async def item_candidates(
    item_id: str,
    client: JellyfinDep,
    session: Annotated[AsyncSession, Depends(get_session)],
    body: Annotated[CandidatesRequest, Body()] = CandidatesRequest(),
) -> dict[str, Any]:
    """Archived Last.fm entities that could be this item, best match first.

    Reads only; nothing is fetched and nothing is written.
    """
    dto = await client.item(item_id)
    item = normalise(dto)
    resolved = await resolve_candidates(session, item, limit=body.limit)
    return {
        "item_id": item_id,
        "kind": item.kind,
        "name": item.name,
        "etag": item.etag,
        "candidates": [describe_candidate(candidate) for candidate in resolved],
        "count": len(resolved),
        "note": (
            "Read from the local archive. Nothing was fetched from Last.fm and nothing "
            "was written to Jellyfin."
        ),
    }


@router.post("/items/{item_id}/diff", response_model=DiffResponse)
async def item_diff(
    item_id: str,
    client: JellyfinDep,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    body: Annotated[DiffRequest, Body()],
) -> dict[str, Any]:
    """Preview the exact changes applying this candidate would make.

    Everything is computed and returned; nothing is written. The response includes
    the withheld fields and the reasons, so a policy can be debugged rather than
    guessed at.
    """
    dto = await client.item(item_id)
    plan = await build_plan_for_item(
        session=session,
        item=normalise(dto),
        entity_id=body.entity_id,
        entity_kind=body.entity_kind,
        overrides={override.field: override.mode for override in body.overrides},
        tag_policy=await tag_policy_with_stored_blacklist(session, settings, body.tag_policy),
    )
    return plan.as_dict()


@router.post("/items/{item_id}/apply", response_model=ApplyResponse)
async def item_apply(
    item_id: str,
    client: JellyfinDep,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    body: Annotated[ApplyRequest, Body()],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, Any]:
    """Apply a reviewed change set.

    ``confirm`` must be true. A write is never the default outcome of a request, and
    an omitted selection means the plan's default -- which is empty for anything that
    needs review, so an unreviewed match writes nothing.
    """
    if not body.confirm:
        raise ValidationError(
            "Refusing to apply without confirm=true. Preview the diff first, then "
            "confirm the specific fields you intend to write."
        )

    dto = await client.item(item_id)
    plan = await build_plan_for_item(
        session=session,
        item=normalise(dto),
        entity_id=body.entity_id,
        entity_kind=body.entity_kind,
        overrides={override.field: override.mode for override in body.overrides},
        tag_policy=await tag_policy_with_stored_blacklist(session, settings, body.tag_policy),
    )
    outcome = await apply_plan(
        session=session,
        client=client,
        plan=plan,
        requested=body.fields,
        expected_etag=body.expected_etag,
        user_id=None,
    )
    outcome.idempotency_key = idempotency_key
    return outcome.as_dict()


@router.get("/items/{item_id}/snapshots", response_model=SnapshotListResponse)
async def item_snapshots(
    item_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    """Snapshot history, newest first, so any applied change can be undone."""
    rows = (
        (
            await session.execute(
                select(Snapshot)
                .where(Snapshot.item_id == item_id)
                .order_by(Snapshot.created_at.desc(), Snapshot.id.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return {
        "item_id": item_id,
        "snapshots": [
            {
                "id": row.id,
                "kind": row.kind,
                "name": row.name,
                "source_op": row.source_op,
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "etag": row.etag,
                "field_count": len(row.fields or {}),
            }
            for row in rows
        ],
        "count": len(rows),
    }


@router.post("/snapshots/{snapshot_id}/revert", response_model=ApplyResponse)
async def revert(
    snapshot_id: int,
    client: JellyfinDep,
    session: Annotated[AsyncSession, Depends(get_session)],
    confirm: Annotated[bool, Query(description="must be true")] = False,
) -> dict[str, Any]:
    """Restore the values captured by a snapshot.

    A revert is an ordinary write built from the snapshot's fields, and it is itself
    snapshotted -- so undoing an undo works and history is never mutated.
    """
    if not confirm:
        raise ValidationError("Refusing to revert without confirm=true.")

    snapshot = (
        await session.execute(select(Snapshot).where(Snapshot.id == snapshot_id))
    ).scalar_one_or_none()
    if snapshot is None:
        raise NotFoundError(f"No snapshot with id {snapshot_id}")

    outcome: ApplyOutcome = await revert_snapshot(session=session, client=client, snapshot=snapshot)
    return outcome.as_dict()


# --------------------------------------------------------------------- helpers


def tag_policy_from(request: TagPolicyRequest | None) -> TagPolicy:
    """The policy for one request, from the request body alone.

    Synchronous, and therefore *without* the stored blacklist: ``blacklist=`` is left at
    its empty default here and filled in by :func:`tag_policy_with_stored_blacklist`. The
    split exists because four call sites need the stored list and several unit tests need
    a body-only policy, and threading a session through all of them to serve the first
    group would make the pure path untestable.
    """
    if request is None:
        return TagPolicy()
    return TagPolicy(
        genre_limit=request.genre_limit,
        style_limit=request.style_limit,
        min_count=request.min_count,
        extra_blacklist=frozenset(name.casefold() for name in request.extra_blacklist),
    )


async def tag_policy_with_stored_blacklist(
    session: AsyncSession,
    settings: Settings,
    request: TagPolicyRequest | None,
) -> TagPolicy:
    """A request's policy, with the operator's stored blacklist enforced.

    Every endpoint that *builds a plan* must use this rather than ``tag_policy_from``, or
    the setting silently does nothing on that path -- which is how a saved entry appears
    to be ignored. The stored values go into ``blacklist`` rather than ``extra_blacklist``
    so that a request body can still add to them without being able to remove them: a
    caller must not be able to talk the server out of the operator's policy.

    Reads the blacklist per request rather than caching it. It is one indexed query over
    tens of rows, and a cached copy would go stale the moment the operator saved a change
    -- reintroducing exactly the "why do I need a restart" problem this replaced.
    """
    policy = tag_policy_from(request)
    stored = await genre_blacklist.effective(session, settings)
    return replace(policy, blacklist=stored)


__all__ = ["router"]
