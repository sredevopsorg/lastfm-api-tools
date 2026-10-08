"""Bulk editing over Server-Sent Events.

Streaming rather than a single response because the operations are long and partly
irreversible: a diff over 200 items should show progress, and an apply must report each
item as it happens so a failure is visible at the item it happened on rather than at the
end of a batch.

SSE rather than WebSockets because the traffic is one-way progress, and it survives
proxies that would complicate an upgrade -- which matters for a self-hosted tool often
sitting behind a reverse proxy.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from metaedit.api.deps import JellyfinDep
from metaedit.api.items import FieldPolicyOverride, TagPolicyRequest, tag_policy_from
from metaedit.api.schemas import BulkJobListResponse, BulkStreamEvent
from metaedit.api.sse import error_frame, sse_frame
from metaedit.db.session import get_session
from metaedit.domain.errors import NotFoundError, ValidationError
from metaedit.service import bulk

router = APIRouter(prefix="/bulk", tags=["bulk"])

# A batch is capped well below what the server could return, because the failure mode
# of a too-large batch is not slowness but a library-wide wrong edit.
MAX_ITEMS = bulk.MAX_BATCH_ITEMS

SelectionKind = Literal["artist", "album", "song"]


class BulkSelection(BaseModel):
    """Which items, and nothing else. The fields to write are chosen after review."""

    kind: SelectionKind = "artist"
    ids: list[str] | None = Field(default=None, description="explicit item ids, if known")
    parent_id: str | None = None
    search: str | None = None
    limit: int = Field(default=50, ge=1, le=MAX_ITEMS)
    missing_metadata: bool = Field(
        default=False, description="only items lacking genres, provider ids or an overview"
    )


class BulkDiffRequest(BaseModel):
    selection: BulkSelection = BulkSelection()
    overrides: list[FieldPolicyOverride] = Field(default_factory=list)
    tag_policy: TagPolicyRequest | None = None
    # An item whose best match scores below this is reported as skipped, so a batch
    # cannot quietly include guesses.
    min_confidence: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="skip items whose best candidate scores below this",
    )


class BulkApplyRequest(BaseModel):
    job_id: str = Field(description="from a /bulk/diff run")
    fields: list[str] | None = Field(
        default=None,
        description="fields to write for every item; omit to use each item's default "
        "selection, which is empty for anything needing review",
    )
    selections: dict[str, list[str]] | None = Field(
        default=None,
        description="per-item fields, keyed by item id. This is what review produces: an "
        "album and an artist do not share a field set, so one list for both would either "
        "write a field an item should not change or skip one it should. An item absent "
        "from the map writes nothing.",
    )
    confirm: bool = Field(default=False, description="must be true")


async def _stream(events: AsyncIterator[dict[str, Any]]) -> AsyncIterator[str]:
    """Wrap a service generator as SSE, ending with a terminating frame.

    Failures are reported in-band as an ``error`` event rather than by breaking the
    stream, so a client that has already received per-item results keeps them. The
    frame is built by ``api.sse``, which keeps internal detail out of it.
    """
    try:
        async for event in events:
            yield sse_frame(event)
    except Exception as exc:
        # `error_frame` is what decides whether this exception's text is safe to send;
        # a `MetaeditError` keeps its own code and message, anything else is generic.
        yield sse_frame(error_frame(exc, code="internal_error"))
    yield sse_frame({"type": "done"})


@router.post(
    "/diff",
    responses={
        200: {
            "model": BulkStreamEvent,
            "description": "Server-Sent Events, one frame per item.",
            "content": {"text/event-stream": {}},
        }
    },
)
async def bulk_diff(
    client: JellyfinDep,
    session: Annotated[AsyncSession, Depends(get_session)],
    body: Annotated[BulkDiffRequest, Body()] = BulkDiffRequest(),
) -> StreamingResponse:
    """Diff every item in the selection, streaming one event per item.

    **Writes nothing.** The job id in the summary event is what a later ``/bulk/apply``
    must present, so an apply can never be the first thing that happens.
    """
    job = await bulk.build_job(
        session=session,
        client=client,
        kind=body.selection.kind,
        parent_id=body.selection.parent_id,
        search=body.selection.search,
        ids=body.selection.ids,
        limit=body.selection.limit,
        missing_metadata=body.selection.missing_metadata,
        overrides={override.field: override.mode for override in body.overrides},
        tag_policy=tag_policy_from(body.tag_policy),
        min_confidence=body.min_confidence,
    )
    return StreamingResponse(
        _stream(bulk.stream_job_diff(job)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@router.post(
    "/apply",
    responses={
        200: {
            "model": BulkStreamEvent,
            "description": "Server-Sent Events, one frame per item.",
            "content": {"text/event-stream": {}},
        }
    },
)
async def bulk_apply(
    client: JellyfinDep,
    session: Annotated[AsyncSession, Depends(get_session)],
    body: Annotated[BulkApplyRequest, Body()],
) -> StreamingResponse:
    """Apply a reviewed job, streaming progress and isolating failures per item.

    ``confirm`` must be true. The job must exist, which means a diff was reviewed in
    this process: a bulk write is never the first request of a session.
    """
    if not body.confirm:
        raise ValidationError(
            "Refusing to apply without confirm=true. Run /bulk/diff, review the result "
            "and its job_id, then confirm."
        )
    job = bulk.registry().get(body.job_id)
    if job is None:
        raise NotFoundError(
            f"Unknown or expired diff job {body.job_id!r}. Diff jobs are held in memory "
            "and a restart clears them; run /bulk/diff again."
        )

    return StreamingResponse(
        _stream(
            bulk.apply_job(
                session=session,
                client=client,
                job=job,
                fields=body.fields,
                selections=body.selections,
            )
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@router.post(
    "/{batch_id}/revert",
    responses={
        200: {
            "model": BulkStreamEvent,
            "description": "Server-Sent Events, one frame per item.",
            "content": {"text/event-stream": {}},
        }
    },
)
async def bulk_revert(
    batch_id: str,
    client: JellyfinDep,
    session: Annotated[AsyncSession, Depends(get_session)],
    confirm: bool = False,
) -> StreamingResponse:
    """Undo every item written by a batch, oldest first, isolating failures.

    The batch id lives on the snapshots, so this works after a restart even though the
    diff job that produced it does not.
    """
    if not confirm:
        raise ValidationError("Refusing to revert a batch without confirm=true.")
    return StreamingResponse(
        _stream(bulk.revert_batch(session=session, client=client, batch_id=batch_id)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@router.get("/jobs", response_model=BulkJobListResponse)
async def list_jobs() -> dict[str, Any]:
    """Reviewed diffs still available to apply, newest last.

    Exposed because "which batch am I about to apply" should be answerable before an
    apply, not discovered afterwards.
    """
    jobs = bulk.registry().list()
    return {"jobs": [job.summary() for job in jobs], "count": len(jobs)}
