"""Harvest endpoints: fetch Last.fm data for items and store it.

This is the step that closes the flow. Without it the archive could only be filled by
the test suite, so a fresh deployment had nothing to propose and the editor had no
candidate to show.

Two shapes, because the two uses are different:

* ``POST /items/{id}/harvest`` -- one item, plain JSON. The editor's "fetch this one"
  button, where waiting for a stream would be more machinery than the work needs.
* ``POST /harvest`` -- a selection, streamed as Server-Sent Events. A batch spends a
  rate-limited third-party request per method per item, so progress has to be visible
  and a failure has to be attributable to the item it happened on.

Neither writes to Jellyfin. Harvesting only fills the archive; the derived layer is
rebuilt once at the end of a batch so candidates can resolve.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from metaedit.api.deps import JellyfinDep, LastfmDep
from metaedit.api.schemas import HarvestItemResponse, HarvestStreamEvent
from metaedit.api.sse import error_frame, sse_frame
from metaedit.archive.reindex import reindex
from metaedit.db.session import get_session
from metaedit.domain.browse_filters import MissingAspect
from metaedit.domain.errors import NotFoundError
from metaedit.service import bulk, harvest
from metaedit.service.planning import normalise

router = APIRouter(tags=["harvest"])


class HarvestSelection(BaseModel):
    """Which items to fetch for. Mirrors the library browse parameters."""

    kind: Literal["artist", "album", "song"] = "artist"
    ids: list[str] | None = Field(default=None, description="explicit item ids, if known")
    parent_id: str | None = None
    search: str | None = None
    limit: int = Field(default=50, ge=1, le=bulk.MAX_BATCH_ITEMS)
    missing: list[MissingAspect] = Field(
        default_factory=list,
        description=(
            "only items lacking any of these: genres, provider_ids, overview, tags. "
            "Aspects that do not apply to the media type are ignored, so asking a song "
            "selection for `overview` selects nothing rather than everything."
        ),
    )
    # The same three narrowings the batch and the browse accept, so a selection means one
    # thing across the app. Honoured rather than merely accepted: a field the API takes and
    # ignores is worse than one it refuses, because the caller has no way to find out.
    artist_ids: list[str] = Field(
        default_factory=list,
        description="only items credited to these artists; applies to `album` and `song`",
    )
    album_ids: list[str] = Field(
        default_factory=list, description="only songs on these albums; applies to `song`"
    )
    exclude: list[str] = Field(
        default_factory=list,
        description=(
            "case-insensitive glob patterns; an item whose name, album or album artist "
            "matches any of them is dropped"
        ),
    )


class HarvestRequest(BaseModel):
    selection: HarvestSelection = HarvestSelection()
    # A search fallback costs an extra request per miss, so it is offered rather than
    # assumed -- on a large library most misses are obscure and the results are noise.
    search_fallback: bool = True
    # Rebuilding the derived layer is what makes the fetched data usable. It is on by
    # default because a harvest whose results cannot be resolved is not finished work.
    reindex: bool = Field(
        default=True, description="rebuild the derived layer so candidates can resolve"
    )


@router.post("/items/{item_id}/harvest", response_model=HarvestItemResponse)
async def harvest_one(
    item_id: str,
    client: JellyfinDep,
    lastfm: LastfmDep,
    session: Annotated[AsyncSession, Depends(get_session)],
    search_fallback: Annotated[bool, Body(embed=True)] = True,
) -> dict[str, Any]:
    """Fetch and archive Last.fm data for one item.

    Reads Jellyfin, spends Last.fm requests, writes nothing to Jellyfin. A miss is a
    reported outcome with search alternatives, not an error status.
    """
    dto = await client.item(item_id)
    item = normalise(dto)

    outcome = await harvest.harvest_item(
        session=session, client=lastfm, item=item, search_fallback=search_fallback
    )
    await session.commit()

    report = await reindex(session)
    await session.commit()
    payload = outcome.as_dict()
    payload["derived"] = report.as_dict()["counts"]
    return payload


async def _stream(
    events: AsyncIterator[dict[str, Any]], *, reindex_after: bool, session: AsyncSession
) -> AsyncIterator[str]:
    """Wrap a harvest generator as SSE, rebuilding the derived layer at the end.

    The reindex runs here rather than per item because it is a pass over the whole raw
    layer; inside the loop it would make a batch quadratic. A failure is reported
    in-band so the client keeps the per-item results it already received.
    """
    try:
        async for event in events:
            yield sse_frame(event)
        if reindex_after:
            report = await reindex(session)
            await session.commit()
            yield sse_frame({"type": "reindexed", **report.as_dict()["counts"]})
    except Exception as exc:
        # See `api/sse.py`: the frame builder decides what is safe to disclose.
        yield sse_frame(error_frame(exc, code="harvest_failed"))
    yield sse_frame({"type": "done"})


@router.post(
    "/harvest",
    responses={
        200: {
            "model": HarvestStreamEvent,
            "description": "Server-Sent Events, one frame per harvested item.",
            "content": {"text/event-stream": {}},
        }
    },
)
async def harvest_batch(
    client: JellyfinDep,
    lastfm: LastfmDep,
    session: Annotated[AsyncSession, Depends(get_session)],
    body: Annotated[HarvestRequest, Body()] = HarvestRequest(),
) -> StreamingResponse:
    """Fetch and archive Last.fm data for a selection, streaming per-item outcomes.

    Writes nothing to Jellyfin. Items are reported as they complete, so a run over a
    large selection shows progress rather than appearing to hang, and an item that
    cannot be found is reported without stopping the batch.
    """
    outcome = await bulk.select_items(
        client,
        kind=body.selection.kind,
        parent_id=body.selection.parent_id,
        search=body.selection.search,
        ids=body.selection.ids,
        limit=body.selection.limit,
        missing=body.selection.missing,
        artist_ids=body.selection.artist_ids,
        album_ids=body.selection.album_ids,
        exclude=body.selection.exclude,
    )
    if not outcome.items:
        raise NotFoundError("The selection matched no items, so there is nothing to fetch.")

    items = [normalise(dto) for dto in outcome.items]

    async def events() -> AsyncIterator[dict[str, Any]]:
        found = 0
        for index, item in enumerate(items):
            outcome = await harvest.harvest_item(
                session=session,
                client=lastfm,
                item=item,
                search_fallback=body.search_fallback,
            )
            await session.commit()
            found += 1 if outcome.found else 0
            yield {"type": "item", "index": index, "total": len(items), **outcome.as_dict()}
        yield {
            "type": "summary",
            "items": len(items),
            "found": found,
            "missing": len(items) - found,
        }

    return StreamingResponse(
        _stream(events(), reindex_after=body.reindex, session=session),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


__all__ = ["router"]
