"""Bulk editing: reviewing and applying many items as one auditable batch.

Bulk is where a mapping mistake stops being a curiosity and becomes a library-wide
event, so the safety model from the single-item path is kept and extended:

* **Review is mandatory.** A batch cannot be applied without a job id from a
  ``/bulk/diff`` run, so "apply to 500 items" is never the first thing that happens.
* **Only pre-selected fields are written.** A field is pre-selected only when the
  match was confident enough to act on unreviewed; everything else stays untouched.
  An item with no trustworthy candidate is skipped, not guessed at.
* **Failures are isolated per item.** One item failing must not abandon the batch
  half-applied with no record of where it stopped.
* **Every write in a batch shares a ``batch_id``**, so the whole run can be reverted
  as one unit even though each item was written separately.

**Diff jobs are process-local and deliberately not persisted.** A diff is a pure
function of the archive plus the current item state, so it is cheap to recompute;
storing it would add a table whose contents go stale the moment anything is edited.
The batch *identity* that matters for undo is persisted, on the snapshots themselves,
which is why batch revert survives a restart while a diff job does not.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from metaedit.adapters.jellyfin.client import JellyfinClient
from metaedit.db.models import Snapshot
from metaedit.domain.browse_filters import SummaryLike, is_missing, normalise_aspects
from metaedit.domain.confidence import Confidence
from metaedit.domain.diff import DiffPlan
from metaedit.domain.errors import MetaeditError, ValidationError, public_error_text
from metaedit.domain.mapping import Candidate, MappingResult, Mode
from metaedit.domain.tags import TagPolicy
from metaedit.domain.writable import ItemKind
from metaedit.logging import get_logger
from metaedit.service.apply import ApplyOutcome, apply_plan, revert_snapshot
from metaedit.service.planning import (
    ENTITY_KIND_BY_ITEM_KIND,
    ITEM_KIND_BY_QUERY,
    QUERY_KINDS,
    build_best_plan,
)

log = get_logger(__name__)

# A guard rail, not a technical limit: a batch bigger than this is almost certainly a
# mis-specified query, and the operator should narrow it deliberately.
MAX_BATCH_ITEMS = 500

# The browse vocabulary is defined in the planning service, next to the archive map it is
# deliberately different from. Re-exported here because this module's callers already
# import from it.
__all__ = ["ITEM_KIND_BY_QUERY", "MAX_BATCH_ITEMS", "QUERY_KINDS"]


@dataclass(frozen=True, slots=True)
class BulkItem:
    """One item in a batch, with the diff that was reviewed for it."""

    item_id: str
    name: str
    kind: str
    plan: DiffPlan
    skipped_reason: str | None = None

    @property
    def applicable(self) -> bool:
        return self.skipped_reason is None and bool(self.plan.default_selection)


@dataclass(slots=True)
class BulkJob:
    """A reviewed batch, held in memory between the diff and the apply."""

    job_id: str
    batch_id: str
    created_at: datetime
    requested: dict[str, Any]
    items: list[BulkItem] = field(default_factory=list)
    applied: bool = False

    @property
    def applicable_items(self) -> list[BulkItem]:
        return [item for item in self.items if item.applicable]

    def summary(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "batch_id": self.batch_id,
            "items": len(self.items),
            "applicable": len(self.applicable_items),
            "skipped": len(self.items) - len(self.applicable_items),
            "created_at": self.created_at.isoformat(),
            "applied": self.applied,
        }


class JobRegistry:
    """In-memory store of reviewed batches.

    Process-local by design (see the module docstring). Bounded so a long-lived
    process cannot accumulate jobs nobody will apply.
    """

    def __init__(self, *, max_jobs: int = 50) -> None:
        self._jobs: dict[str, BulkJob] = {}
        self._order: list[str] = []
        self._max_jobs = max_jobs

    def add(self, job: BulkJob) -> None:
        self._jobs[job.job_id] = job
        self._order.append(job.job_id)
        while len(self._order) > self._max_jobs:
            oldest = self._order.pop(0)
            self._jobs.pop(oldest, None)

    def get(self, job_id: str) -> BulkJob | None:
        return self._jobs.get(job_id)

    def list(self) -> list[BulkJob]:
        """Jobs still available to apply, oldest first."""
        return [self._jobs[job_id] for job_id in self._order if job_id in self._jobs]

    def clear(self) -> None:
        """Test hook."""
        self._jobs.clear()
        self._order.clear()


_registry = JobRegistry()


def registry() -> JobRegistry:
    return _registry


async def select_items(
    client: JellyfinClient,
    *,
    kind: str,
    parent_id: str | None = None,
    search: str | None = None,
    ids: Iterable[str] | None = None,
    limit: int = 50,
    missing: Iterable[str] | None = None,
) -> list[Any]:
    """The items this batch will consider. Ordering is the server's, so it is stable.

    ``missing`` names the aspects to filter on, per media type, and uses the same
    definition as the library browse. The boolean this replaced -- "missing metadata" --
    lumped genres, provider ids and overview together for every media type, which over a
    song library matched almost everything: a song without an overview is the normal state
    of a song (measured on a live library, 5,441 of 5,442 songs have none).
    """
    if limit > MAX_BATCH_ITEMS:
        raise ValidationError(
            f"limit {limit} exceeds the {MAX_BATCH_ITEMS} item batch cap; narrow the query"
        )
    explicit = [item_id for item_id in (ids or []) if item_id]
    if explicit:
        return list(await client.items_by_ids(explicit[:limit]))

    item_kind = ITEM_KIND_BY_QUERY.get(kind)
    if item_kind is None:
        raise ValidationError(
            f"unknown selection kind {kind!r}; expected one of {sorted(ITEM_KIND_BY_QUERY)}"
        )
    result = await client.items(
        kind=item_kind, parent_id=parent_id, search_term=search, limit=limit
    )
    items = list(result.Items)
    aspects = normalise_aspects(item_kind, frozenset(missing or ()))
    if aspects:
        # Jellyfin has no music-library filter for this, so it is applied to the page --
        # and skipping an item is honest here, because the batch reports it as skipped
        # rather than quietly omitting it.
        items = [item for item in items if is_missing(item_kind, aspects, _aspects_of(item))]
    return items


def _aspects_of(dto: Any) -> SummaryLike:
    """The slice of a Jellyfin DTO the missing-filter reads.

    A small adapter rather than teaching the domain about ``BaseItemDto``: the filter is
    shared with the library browse, which feeds it item *summaries*, and those are a
    different type with the same four fields. Narrowing here keeps the comparison in one
    place -- two implementations of "has no genres" is how the browse and the bulk editor
    would come to disagree about which items need work.
    """
    return _DtoAspects(
        genres=list(dto.Genres or []),
        tags=list(dto.Tags or []),
        has_provider_ids=bool(dto.ProviderIds),
        has_overview=bool((dto.Overview or "").strip()),
    )


@dataclass(frozen=True, slots=True)
class _DtoAspects:
    genres: list[str]
    tags: list[str]
    has_provider_ids: bool
    has_overview: bool


async def build_job(
    *,
    session: AsyncSession,
    client: JellyfinClient,
    kind: str,
    parent_id: str | None = None,
    search: str | None = None,
    ids: Iterable[str] | None = None,
    limit: int = 50,
    missing: Sequence[str] | None = None,
    overrides: dict[str, Mode] | None = None,
    tag_policy: TagPolicy | None = None,
    min_confidence: float = 0.0,
) -> BulkJob:
    """Diff every item in the selection, recording why any was skipped.

    Nothing is written. An item with no archived candidate, or one whose best match is
    below ``min_confidence``, is included in the job as skipped rather than omitted --
    silently dropping items would make a batch look complete when it was not.
    """
    dtos = await select_items(
        client,
        kind=kind,
        parent_id=parent_id,
        search=search,
        ids=ids,
        limit=limit,
        missing=missing,
    )
    job = BulkJob(
        job_id=uuid.uuid4().hex,
        batch_id=uuid.uuid4().hex,
        created_at=datetime.now(UTC),
        requested={
            "kind": kind,
            "parent_id": parent_id,
            "search": search,
            "limit": limit,
            "missing": list(missing or ()),
            "min_confidence": min_confidence,
        },
    )

    for dto in dtos:
        try:
            plan = await build_best_plan(
                session=session,
                client=client,
                dto=dto,
                overrides=overrides or {},
                tag_policy=tag_policy or TagPolicy(),
            )
        except MetaeditError as exc:
            job.items.append(
                BulkItem(
                    item_id=dto.Id or "",
                    name=dto.Name or "",
                    kind=dto.Type or "",
                    plan=_empty_plan(dto),
                    skipped_reason=exc.message,
                )
            )
            continue

        skipped: str | None = None
        if plan.confidence.mbid_conflict:
            skipped = "MusicBrainz ids conflict, so this candidate is a different entity"
        elif plan.confidence.total < min_confidence:
            skipped = (
                f"confidence {plan.confidence.total:.2f} is below the "
                f"{min_confidence:.2f} threshold"
            )
        elif not plan.proposed:
            skipped = "nothing to propose: the item already matches"

        job.items.append(
            BulkItem(
                item_id=dto.Id or "",
                name=dto.Name or "",
                kind=dto.Type or "",
                plan=plan,
                skipped_reason=skipped,
            )
        )

    _registry.add(job)
    log.info("bulk_job_built", **job.summary())
    return job


async def stream_job_diff(job: BulkJob) -> AsyncIterator[dict[str, Any]]:
    """Yield the reviewed diff for each item, one event at a time."""
    for index, item in enumerate(job.items):
        yield {
            "type": "item",
            "index": index,
            "item_id": item.item_id,
            "name": item.name,
            "kind": item.kind,
            "skipped_reason": item.skipped_reason,
            "diff": item.plan.as_dict(),
            "applicable": item.applicable,
        }
    yield {"type": "summary", **job.summary()}


async def apply_job(
    *,
    session: AsyncSession,
    client: JellyfinClient,
    job: BulkJob,
    fields: list[str] | None = None,
    selections: Mapping[str, Sequence[str]] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Apply a reviewed job, isolating failures per item.

    Each item is committed independently: a failure on item 37 must not roll back the
    36 already written, because the operator needs to see exactly where it stopped and
    the batch revert must still be able to undo what did happen.

    ``fields`` applies the same field set to every item; ``selections`` gives a per-item
    set keyed by item id. The per-item form is what review actually produces -- an album
    and an artist do not have the same fields, and forcing one list onto both would mean
    either writing a field an item has no business changing or silently skipping one it
    should. An item absent from ``selections`` falls back to its own default, which is
    empty for anything needing review, so an omitted item writes nothing.
    """

    def requested_for(item: BulkItem) -> list[str] | None:
        if selections is None:
            return fields
        chosen = selections.get(item.item_id)
        if chosen is None:
            return _default_selection_for(job, item)
        return list(chosen)

    if job.applied:
        raise ValidationError(
            f"job {job.job_id} has already been applied; build a new diff to run again"
        )

    targets = job.applicable_items
    applied: list[str] = []
    failed: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []

    for item in job.items:
        if not item.applicable:
            skipped.append(
                {"item_id": item.item_id, "name": item.name, "reason": item.skipped_reason or ""}
            )

    for index, item in enumerate(targets):
        try:
            outcome = await _apply_one(
                session=session,
                client=client,
                plan=item.plan,
                requested=requested_for(item),
                batch_id=job.batch_id,
            )
        except Exception as exc:
            # Roll back only this item's work, leaving prior items committed.
            #
            # Catching `Exception` rather than `MetaeditError` is deliberate. Items are
            # committed independently precisely so that one failure does not discard
            # the rest, and that promise cannot hold if it only covers failures we
            # anticipated: an unexpected error would escape this loop with the session
            # still dirty, the next item's flush would raise `PendingRollbackError`,
            # and the batch would stop while the real cause was buried under a second
            # error. The rollback belongs at the per-item boundary, which is here.
            await session.rollback()
            reason = public_error_text(exc)
            failed.append({"item_id": item.item_id, "name": item.name, "error": reason})
            yield {
                "type": "failed",
                "index": index,
                "item_id": item.item_id,
                "name": item.name,
                "error": reason,
                "error_code": exc.code if isinstance(exc, MetaeditError) else "internal_error",
            }
            continue

        applied.append(item.item_id)
        yield {
            "type": "applied",
            "index": index,
            "item_id": item.item_id,
            "name": item.name,
            "applied_fields": outcome.applied,
            "snapshot_id": outcome.snapshot_id,
        }

    job.applied = True
    yield {
        "type": "summary",
        "job_id": job.job_id,
        "batch_id": job.batch_id,
        "applied": len(applied),
        "failed": len(failed),
        "skipped": len(skipped),
        "failures": failed,
        "batch_revert": f"/api/bulk/{job.batch_id}/revert",
    }


def _default_selection_for(job: BulkJob, item: BulkItem) -> list[str]:
    """What an item would write if nobody ticked anything: its safe default.

    Empty for anything that needs review, which is what makes an un-reviewed item a
    no-op rather than an uncontrolled write.
    """
    del job
    return list(item.plan.default_selection)


async def _apply_one(
    *,
    session: AsyncSession,
    client: JellyfinClient,
    plan: DiffPlan,
    requested: list[str] | None,
    batch_id: str,
) -> ApplyOutcome:
    """Apply one item and stamp its snapshot with the batch id."""
    outcome = await apply_plan(
        session=session, client=client, plan=plan, requested=requested, user_id=None
    )
    if outcome.snapshot_id is not None:
        snapshot = (
            await session.execute(select(Snapshot).where(Snapshot.id == outcome.snapshot_id))
        ).scalar_one_or_none()
        if snapshot is not None:
            snapshot.batch_id = batch_id
            await session.commit()
    return outcome


async def batch_snapshots(session: AsyncSession, batch_id: str) -> list[Snapshot]:
    """Every snapshot belonging to a batch, oldest first so reverting unwinds in order."""
    rows = (
        await session.execute(
            select(Snapshot)
            .where(Snapshot.batch_id == batch_id, Snapshot.source_op == "apply")
            .order_by(Snapshot.id)
        )
    ).scalars()
    return list(rows)


async def revert_batch(
    *, session: AsyncSession, client: JellyfinClient, batch_id: str
) -> AsyncIterator[dict[str, Any]]:
    """Undo a whole batch, one item at a time, isolating failures.

    Reverting oldest-first means an item touched twice within a batch ends up at its
    original state, which is what "undo the batch" has to mean.
    """
    snapshots = await batch_snapshots(session, batch_id)
    if not snapshots:
        raise ValidationError(f"no applied snapshots found for batch {batch_id}")

    reverted = 0
    failed: list[dict[str, str]] = []
    for snapshot in snapshots:
        try:
            outcome = await revert_snapshot(session=session, client=client, snapshot=snapshot)
        except MetaeditError as exc:
            await session.rollback()
            failed.append({"item_id": snapshot.item_id, "error": public_error_text(exc)})
            yield {
                "type": "failed",
                "item_id": snapshot.item_id,
                "error": public_error_text(exc),
                "error_code": exc.code,
            }
            continue
        reverted += 1
        yield {
            "type": "reverted",
            "item_id": snapshot.item_id,
            "restored_fields": outcome.applied,
        }

    yield {
        "type": "summary",
        "batch_id": batch_id,
        "reverted": reverted,
        "failed": len(failed),
        "failures": failed,
    }


def _empty_plan(dto: Any) -> DiffPlan:
    """A placeholder plan for a skipped item, so the response shape never varies."""
    kind: ItemKind = dto.Type if dto.Type in ENTITY_KIND_BY_ITEM_KIND else "MusicArtist"
    return DiffPlan(
        item_id=dto.Id or "",
        kind=kind,
        item_name=dto.Name or "",
        candidate=Candidate(kind=kind, name=dto.Name or ""),
        confidence=Confidence(total=0.0, verdict="reject", components=[], notes=[]),
        mapping=MappingResult(),
    )
