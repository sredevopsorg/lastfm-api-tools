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

from collections.abc import AsyncIterator, Sequence
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from metaedit.api.deps import JellyfinDep
from metaedit.api.items import (
    FieldPolicyOverride,
    TagPolicyRequest,
    tag_policy_with_stored_blacklist,
)
from metaedit.api.schemas import (
    BulkJobListResponse,
    BulkStreamEvent,
    GenreVocabularyResponse,
    RemovalJobListResponse,
    RemovalStreamEvent,
)
from metaedit.api.sse import error_frame, sse_frame
from metaedit.config import Settings, get_settings
from metaedit.db.session import get_session
from metaedit.domain.browse_filters import MissingAspect
from metaedit.domain.errors import NotFoundError, ValidationError
from metaedit.service import bulk, genre_removal

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
    missing: list[MissingAspect] = Field(
        default_factory=list,
        description=(
            "only items lacking any of these: genres, provider_ids, overview, tags. "
            "Aspects that do not apply to the media type are ignored, so asking a song "
            "selection for `overview` selects nothing rather than everything."
        ),
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
    settings: Annotated[Settings, Depends(get_settings)],
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
        missing=body.selection.missing,
        overrides={override.field: override.mode for override in body.overrides},
        tag_policy=await tag_policy_with_stored_blacklist(session, settings, body.tag_policy),
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


# --------------------------------------------------------------- genre removal
#
# A second kind of batch that shares this module's write path. Kept under /bulk because
# that is what it is -- a reviewed, snapshot-backed, revertible batch -- and because
# `/{batch_id}/revert` already serves it, so a removal run is undoable by the same call
# as any other batch.


def _default_removal_fields() -> list[Literal["Genres", "Tags"]]:
    """`Genres` by default.

    A named function rather than a lambda because pydantic's `default_factory` is typed
    against the declared element type, and a lambda returning `list[str]` does not satisfy
    `list[Literal["Genres", "Tags"]]` -- the annotation here is what makes it fit.
    """
    return ["Genres"]


class GenreRemovalSelection(BaseModel):
    """Which items to look in.

    No `missing` filter here, unlike `BulkSelection`: this tool selects by *having* a
    genre, which Jellyfin can express natively and cheaply, rather than by lacking
    something, which it cannot.
    """

    kind: SelectionKind = "artist"
    ids: list[str] | None = Field(default=None, description="explicit item ids, if known")
    parent_id: str | None = None
    search: str | None = None
    limit: int = Field(default=100, ge=1, le=MAX_ITEMS)


class GenreRemovalRequest(BaseModel):
    """The value to remove, and where."""

    selection: GenreRemovalSelection = GenreRemovalSelection()
    genre: str = Field(
        description="the genre value to remove. Matched exactly and case-insensitively; "
        "a blank value is refused rather than treated as a wildcard.",
    )
    fields: list[Literal["Genres", "Tags"]] = Field(
        default_factory=_default_removal_fields,
        description="which arrays to remove from. Both are written by this application.",
    )
    decompose: bool = Field(
        default=False,
        description="also remove matching parts of a packed value ('Rock, Reggae' with "
        "genre 'Reggae' becomes 'Rock'). Off by default: it is the destructive reading of "
        "a value that may be one genre whose name contains a separator.",
    )


class GenreRemovalApplyRequest(BaseModel):
    job_id: str = Field(description="from a /bulk/remove-genre/diff run")
    selections: dict[str, list[str]] | None = Field(
        default=None,
        description="per-item fields, keyed by item id. An item absent from the map writes "
        "nothing, so an unreviewed item is a no-op.",
    )
    confirm: bool = Field(default=False, description="must be true")


def _removal_selection(
    body: GenreRemovalSelection,
    *,
    genre: str,
    fields: Sequence[str],
    decompose: bool,
) -> genre_removal.RemovalSelection:
    """Translate the request body into the service's selection value."""
    return genre_removal.RemovalSelection(
        kind=body.kind,
        parent_id=body.parent_id,
        search=body.search,
        ids=body.ids,
        limit=body.limit,
        target=genre,
        fields=tuple(fields or ("Genres",)),
        decompose=decompose,
    )


@router.post(
    "/remove-genre/diff",
    responses={
        200: {
            "model": RemovalStreamEvent,
            "description": "Server-Sent Events, one frame per item.",
            "content": {"text/event-stream": {}},
        }
    },
)
async def remove_genre_diff(
    client: JellyfinDep,
    body: Annotated[GenreRemovalRequest, Body()],
) -> StreamingResponse:
    """Find every item carrying a genre, and show what removing it would do.

    **Writes nothing.** The job id in the summary is what a later apply must present, so a
    library-wide removal can never be the first thing a session does.

    Selection uses Jellyfin's own ``Genres``/``Tags`` filter, which is an exact,
    case-insensitive, server-side match -- verified live. So the reported count is real
    and there is no scan to truncate.
    """
    job = await genre_removal.build_job(
        client=client,
        selection=_removal_selection(
            body.selection,
            genre=body.genre,
            fields=body.fields,
            decompose=body.decompose,
        ),
    )
    return StreamingResponse(
        _stream(genre_removal.stream_job_diff(job)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@router.post(
    "/remove-genre/apply",
    responses={
        200: {
            "model": RemovalStreamEvent,
            "description": "Server-Sent Events, one frame per item.",
            "content": {"text/event-stream": {}},
        }
    },
)
async def remove_genre_apply(
    client: JellyfinDep,
    session: Annotated[AsyncSession, Depends(get_session)],
    body: Annotated[GenreRemovalApplyRequest, Body()],
) -> StreamingResponse:
    """Apply a reviewed removal, streaming progress and isolating failures per item.

    ``confirm`` must be true and the job must exist, so a removal is never the first
    request of a session. Every write is snapshotted and shares the job's ``batch_id``,
    so the whole run is undoable through ``/{batch_id}/revert``.
    """
    if not body.confirm:
        raise ValidationError(
            "Refusing to remove a genre without confirm=true. Run /bulk/remove-genre/diff, "
            "review the result and its job_id, then confirm."
        )
    job = await genre_removal.get_removal_job_or_404(body.job_id)

    return StreamingResponse(
        _stream(
            genre_removal.apply_removal_job(
                session=session, client=client, job=job, selections=body.selections
            )
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@router.get("/remove-genre/jobs", response_model=RemovalJobListResponse)
async def list_removal_jobs() -> dict[str, Any]:
    """Reviewed removals still available to apply."""
    jobs = genre_removal.removal_registry().list()
    return {"jobs": [job.summary() for job in jobs], "count": len(jobs)}


@router.get("/genres", response_model=GenreVocabularyResponse)
async def library_genres(
    client: JellyfinDep,
    item_kind: Annotated[
        Literal["MusicArtist", "MusicAlbum", "Audio"],
        Query(description="which media type's genre set to list; the sets differ"),
    ] = "MusicArtist",
) -> dict[str, Any]:
    """The genre vocabulary this library actually uses.

    Read from Jellyfin's genre entities rather than by scanning items: it is the same list
    the server's own genre filter offers (39 entries for this library's artists), so what
    the operator picks here is what they would pick there. Read-only.

    Scoped per media type because the sets genuinely differ -- an album-only genre is not
    an artist genre, and listing one type's genres for another would offer a value that
    matches nothing.
    """
    entities = await client.genres(item_kind=item_kind)
    names = sorted({entity.Name for entity in entities if entity.Name})
    return {
        "genres": names,
        "count": len(names),
        "note": (
            "The library's genre entities, which is what Jellyfin's own filter lists. "
            "Removal matches a value exactly and case-insensitively."
        ),
    }
