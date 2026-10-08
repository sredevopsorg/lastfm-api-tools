"""Removing one genre from many items, as one auditable batch.

Reuses the bulk lifecycle rather than inventing a second one: a reviewed set of plans, a
job that must exist before an apply, per-item failure isolation, and a shared ``batch_id``
so the whole run reverts as a unit. The only new thing is *how* the plans are built --
from a value the operator names, instead of from a Last.fm candidate.

That reuse is what makes this safe. Every write still goes through ``apply_plan``, so it
inherits, without restating:

* **payload completeness** -- ``to_payload`` carries every unselected field through at its
  current value, so a genre removal can never null the overview (ADR 0003);
* **snapshot before write** -- so any removal is reversible in one call (ADR 0004);
* **the etag check** -- so a removal cannot silently revert an edit made since the review
  (ADR 0007);
* **an audit row** -- so provenance exists for a change Last.fm had no part in.

A removal is deliberately *not* a merge. ``replace`` mode sets the field to the computed
list, because leaving a genre "because merge mode preserves existing values" would mean
the tool never removed anything.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from metaedit.adapters.jellyfin.client import JellyfinClient
from metaedit.domain.confidence import Confidence
from metaedit.domain.diff import DiffPlan, build_plan
from metaedit.domain.errors import NotFoundError, ValidationError
from metaedit.domain.genre_removal import (
    REMOVAL_FIELDS,
    RemovalOutcome,
    plan_removals,
    remove_component,
)
from metaedit.domain.mapping import (
    Candidate,
    FieldChange,
    FieldPolicy,
    MappingResult,
)
from metaedit.domain.snapshot import NormalizedItem, from_dto
from metaedit.logging import get_logger
from metaedit.service.bulk import MAX_BATCH_ITEMS, BulkItem
from metaedit.service.planning import item_kind_for
from metaedit.service.selection import Selection, SelectionOutcome

log = get_logger(__name__)


def _fields_for(requested: Iterable[str] | None) -> tuple[str, ...]:
    """Validate the requested fields against the closed set this tool may write.

    A closed set rather than free text: this endpoint writes to Jellyfin, and a caller
    naming ``Overview`` must not be able to reach a field the tool was not designed to
    clear. ``remove_value`` on an arbitrary field would be a general-purpose "delete this
    array element" tool pointed at the whole item schema.
    """
    if not requested:
        return ("Genres",)
    fields: list[str] = []
    for name in requested:
        if name not in REMOVAL_FIELDS:
            raise ValidationError(
                f"cannot remove a genre from {name!r}; expected one of {list(REMOVAL_FIELDS)}"
            )
        if name not in fields:
            fields.append(name)
    # Stable order, so a diff's field order does not depend on how the body was written.
    return tuple(name for name in REMOVAL_FIELDS if name in fields)


def _validate_target(target: str | None) -> str:
    """Refuse an empty target rather than treating it as a wildcard.

    This is the one input here whose failure mode is library-wide. An empty match would
    remove *every* genre from every selected item, and the operator would have asked for
    nothing in particular. Refusing at the boundary is cheap; the alternative is a
    revertable but enormous surprise.
    """
    value = (target or "").strip()
    if not value:
        raise ValidationError(
            "Refusing to remove a blank genre: an empty value would match every genre "
            "on every selected item. Name the genre to remove."
        )
    return value


def plan_for_item(
    item: NormalizedItem,
    *,
    target: str,
    fields: tuple[str, ...],
    decompose: bool = False,
) -> tuple[DiffPlan, list[RemovalOutcome]]:
    """Build a reviewable plan that removes ``target`` from one item.

    Returns the plan and the per-field outcomes, because the *report* is as important as
    the write: an operator needs to see which spellings disappeared and whether a field
    was emptied, not just that the item was "changed".
    """
    initial = plan_removals(item.fields, target=target, fields=fields)
    changes: list[FieldChange] = []
    skipped: list[FieldChange] = []
    # The list that is *returned*, and it is rebuilt rather than mutated. Decomposition
    # changes what the outcome says (`after` and `removed`), and returning the pre-split
    # list would report "nothing removed, after: ['Rock, Reggae', 'Jazz']" for a plan that
    # genuinely writes ['Rock', 'Jazz'] -- the report and the write disagreeing, which is
    # exactly the kind of discrepancy that makes an operator stop trusting a tool.
    outcomes: list[RemovalOutcome] = []

    for outcome in initial:
        proposed = list(outcome.after)
        if decompose:
            proposed, extra = _decomposed(item, outcome=outcome, target=target)
            outcome = RemovalOutcome(
                field=outcome.field,
                target=outcome.target,
                before=outcome.before,
                after=proposed,
                removed=[*outcome.removed, *extra],
            )
        outcomes.append(outcome)
        policy = _policy_for(outcome, decompose=decompose)
        change = FieldChange(
            field=outcome.field,
            current=outcome.before,
            proposed=proposed,
            mode="replace",
            reason=policy.reason,
            source="operator.removal",
            # Pre-selected when it would change something: unlike a Last.fm match, this
            # change is the operator's own explicit instruction, so making them tick it
            # again would be ceremony rather than review.
            selected=outcome.changed,
            provenance={"source": "operator", "target": target, "decompose": decompose},
        )
        (changes if outcome.changed else skipped).append(change)

    mapping = MappingResult(changes=changes, skipped=skipped)
    plan = build_plan(
        item=item,
        candidate=Candidate(kind=item.kind, name=item.name),
        # Not a Last.fm match, so there is no match to score. A perfect score here would
        # be a lie that the UI would render as a green "auto" badge.
        confidence=_operator_confidence(),
        mapping=mapping,
        locked_fields=item.get("LockedFields") or [],
    )
    return plan, outcomes


def _decomposed(
    item: NormalizedItem, *, outcome: RemovalOutcome, target: str
) -> tuple[list[str], list[str]]:
    """Decompose packed values, dropping components that match the target.

    Returns the new list and the components removed, so the report names what actually
    disappeared rather than only the value it came from.
    """
    proposed: list[str] = []
    removed: list[str] = []
    for value in outcome.after:
        split = remove_component(value, target=target)
        if not split.changed:
            proposed.append(value)
            continue
        removed.extend(split.removed_components)
        if split.replacement is not None:
            proposed.append(split.replacement)
        # replacement is None when nothing survived, so the value is dropped entirely.
    return proposed, removed


def _policy_for(outcome: RemovalOutcome, *, decompose: bool) -> FieldPolicy:
    how = "and its packed components" if decompose else ""
    return FieldPolicy(
        field=outcome.field,
        mode="replace",
        field_kind="list",
        source="operator.removal",
        reason=(
            f"removes {outcome.target!r} {how}".strip()
            + f" from {outcome.field} ({len(outcome.removed)} value(s) affected)"
        ),
    )


def _operator_confidence() -> Confidence:
    """The confidence recorded for an operator-driven change.

    ``verdict="auto"``, and that is not a claim about match quality -- it is what makes the
    change *applicable*. ``DiffPlan.default_selection`` returns nothing unless the verdict
    accepts by default, so a "review" verdict here would produce a plan whose selection is
    always empty: a removal that reported every item as skipped and wrote nothing.

    The distinction the confidence model usually draws -- "did we match the right Last.fm
    entity" -- does not apply, because nothing was matched. The operator named the value
    and the field; their instruction is the authority, which is exactly what "auto" means
    for this plan. ``notes`` says so, so a UI showing the verdict has the context.
    """
    return Confidence(
        total=1.0,
        verdict="auto",
        components=[],
        notes=[
            "Operator-specified removal: no Last.fm candidate is involved, so no match was "
            "scored. The named genre and field are the operator's explicit instruction."
        ],
    )


@dataclass(slots=True)
class RemovalSelection:
    """Where to look, and what to remove."""

    kind: str = "artist"
    parent_id: str | None = None
    search: str | None = None
    ids: list[str] | None = None
    limit: int = 100
    target: str = ""
    fields: tuple[str, ...] = ("Genres",)
    decompose: bool = False
    # The same narrowing a browse query and a batch accept, so one concept means one thing
    # across the app. Excluding is worth more here than anywhere else: "remove this genre
    # from every album" is rarely meant to include the compilations.
    artist_ids: tuple[str, ...] = ()
    album_ids: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()

    def filter_for(self) -> Selection:
        """The shared interpretation of the three narrowings, validated on the way in."""
        return Selection.build(
            kind=self.kind,
            artist_ids=self.artist_ids,
            album_ids=self.album_ids,
            exclude=self.exclude,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "parent_id": self.parent_id,
            "search": self.search,
            "limit": self.limit,
            "target": self.target,
            "fields": list(self.fields),
            "decompose": self.decompose,
            "artist_ids": list(self.artist_ids),
            "album_ids": list(self.album_ids),
            "exclude": list(self.exclude),
        }


@dataclass(slots=True)
class RemovalJob:
    """A reviewed removal run, held in memory between the diff and the apply.

    Structurally satisfies ``bulk.ApplicableJob`` -- the protocol ``apply_job`` declares
    for what it reads -- so both kinds of batch share one write path. A distinct type from
    ``BulkJob`` because the summary differs: a removal reports the target, the fields and
    which items would be emptied, none of which a Last.fm diff has.
    """

    job_id: str
    batch_id: str
    created_at: datetime
    requested: dict[str, Any]
    items: list[BulkItem] = field(default_factory=list)
    outcomes: dict[str, list[RemovalOutcome]] = field(default_factory=dict)
    applied: bool = False
    # What the selection read, and what it cost -- so a run narrowed by an exclusion
    # pattern can say how many items the pattern dropped rather than just coming back short.
    report: SelectionOutcome | None = None

    @property
    def applicable_items(self) -> list[BulkItem]:
        return [item for item in self.items if item.applicable]

    @property
    def emptied(self) -> list[dict[str, str]]:
        """Items where a field would be left empty.

        Reported separately because "0 genres left on this artist" is a legitimate edit
        but a surprising one, and it is the outcome most worth a second look before the
        write.
        """
        found: list[dict[str, str]] = []
        for item in self.items:
            for outcome in self.outcomes.get(item.item_id, []):
                if outcome.emptied:
                    found.append(
                        {"item_id": item.item_id, "name": item.name, "field": outcome.field}
                    )
        return found

    def summary(self) -> dict[str, Any]:
        summary: dict[str, Any] = {
            "job_id": self.job_id,
            "batch_id": self.batch_id,
            "items": len(self.items),
            "applicable": len(self.applicable_items),
            "skipped": len(self.items) - len(self.applicable_items),
            "created_at": self.created_at.isoformat(),
            "applied": self.applied,
            "removing": self.requested.get("target"),
            "excluded": self.report.excluded if self.report is not None else 0,
        }
        if self.report is not None and self.report.scanned is not None:
            # Present only when the library was actually read; see BulkJob.summary.
            summary["scanned"] = self.report.scanned
            summary["truncated"] = self.report.truncated
        return summary


class RemovalJobRegistry:
    """Bounded, process-local store of reviewed removal runs.

    Separate from the bulk registry so a removal job id cannot be presented to
    ``/bulk/apply`` (or the reverse) and misinterpreted. The apply endpoint dispatches on
    which registry the id is found in, and an id that belongs to neither is a clear 404
    rather than a plan written from the wrong kind of job.
    """

    def __init__(self, *, max_jobs: int = 50) -> None:
        self._jobs: dict[str, RemovalJob] = {}
        self._order: list[str] = []
        self._max_jobs = max_jobs

    def add(self, job: RemovalJob) -> None:
        self._jobs[job.job_id] = job
        self._order.append(job.job_id)
        while len(self._order) > self._max_jobs:
            self._jobs.pop(self._order.pop(0), None)

    def get(self, job_id: str) -> RemovalJob | None:
        return self._jobs.get(job_id)

    def list(self) -> list[RemovalJob]:
        return [self._jobs[job_id] for job_id in self._order if job_id in self._jobs]

    def clear(self) -> None:
        """Test hook."""
        self._jobs.clear()
        self._order.clear()


_removal_registry = RemovalJobRegistry()


def removal_registry() -> RemovalJobRegistry:
    return _removal_registry


async def find_items(
    client: JellyfinClient,
    *,
    selection: RemovalSelection,
) -> SelectionOutcome:
    """The items carrying the target genre, and what finding them cost.

    Uses Jellyfin's own ``Genres``/``Tags`` query parameter, which is an exact,
    case-insensitive, server-side filter -- verified live: ``Genres=alternative rock``
    returns 23 artists, and every returned item genuinely carries that value. So ``total``
    is real, paging is over the filtered set, and there is no scan to truncate.

    That is the opposite of the browse ``missing=genres`` filter, which has to read items
    because Jellyfin cannot express "has no genres". Worth stating because the two look
    similar and only one is free.

    The artist/album facets ride alongside that filter, which Jellyfin intersects for free.
    An exclusion pattern cannot, so it is applied to each item as it arrives and *counted*:
    an item dropped by a pattern must not consume the limit, or "25 items" would come back
    as four with nothing to explain the difference.
    """
    if selection.limit > MAX_BATCH_ITEMS:
        raise ValidationError(
            f"limit {selection.limit} exceeds the {MAX_BATCH_ITEMS} item batch cap; "
            "narrow the query or raise the cap deliberately"
        )
    wanted = selection.filter_for()
    if selection.decompose:
        return await _scan_for_component(client, selection=selection, wanted=wanted)
    explicit = [item_id for item_id in (selection.ids or []) if item_id]
    if explicit:
        # An explicit id list still has to be filtered: the caller may have selected
        # items that do not carry the genre, and writing a "removal" to them would be a
        # no-op that the report would have to explain.
        candidates = list(await client.items_by_ids(explicit[: selection.limit]))
        kept = [dto for dto in candidates if wanted.carries(dto) and _carries(dto, selection)]
        return SelectionOutcome(
            items=kept,
            scanned=len(candidates),
            excluded=len(candidates) - len(kept),
        )

    from metaedit.service.planning import ITEM_KIND_BY_QUERY

    item_kind = ITEM_KIND_BY_QUERY.get(selection.kind)
    if item_kind is None:
        raise ValidationError(
            f"unknown selection kind {selection.kind!r}; expected one of "
            f"{sorted(ITEM_KIND_BY_QUERY)}"
        )
    facets = wanted.jellyfin_params()

    # One query per field, then union. Jellyfin *ands* the query parameters it is given,
    # so sending `Genres=x&Tags=x` together would return only items carrying the value in
    # both -- silently excluding every item that has it in one. Measured on the live
    # server: `Genres=alternative rock` returns 23 artists and `Tags=alternative rock`
    # returns 1, with the two sets not nested, so the conjunction would be wrong in both
    # directions. The requested fields are alternatives, so they are queried as such.
    seen: set[str] = set()
    collected: list[Any] = []
    excluded = 0
    scanned = 0
    for field_name in selection.fields:
        offset = 0
        while len(collected) < selection.limit:
            page = await client.items(
                kind=item_kind,
                parent_id=selection.parent_id,
                search_term=selection.search,
                start_index=offset,
                limit=min(200, selection.limit - len(collected) + len(seen)),
                filters={field_name: selection.target, **facets},
            )
            if not page.Items:
                break
            for dto in page.Items:
                key = str(getattr(dto, "Id", "") or id(dto))
                if key in seen:
                    continue
                seen.add(key)
                scanned += 1
                if not wanted.carries(dto):
                    excluded += 1
                    continue
                collected.append(dto)
            offset += len(page.Items)
            if offset >= (page.TotalRecordCount or 0):
                break
        if len(collected) >= selection.limit:
            break
    return SelectionOutcome(
        items=collected[: selection.limit],
        scanned=scanned,
        excluded=excluded,
    )


async def _scan_for_component(
    client: JellyfinClient, *, selection: RemovalSelection, wanted: Selection
) -> SelectionOutcome:
    """Items whose *packed* value contains the target as one of its components.

    A scan is unavoidable here and that is a property of the server, not a shortcut.
    Jellyfin's ``Genres`` parameter matches a whole stored value exactly, so it can find
    ``"Rock, Reggae"`` only when asked for that literal string -- there is no way to ask
    for "any item whose Genres contains the component Reggae". Live-verified: ``Reggae``
    matches 10 albums, none of which store it as a packed component of a longer value.

    So the scan walks the media type's items and tests the decomposition locally, which is
    the same trade the browse screen's ``missing`` filter makes and for the same reason.
    It is bounded by ``MAX_SCAN_ITEMS`` and it reports what it read, because a silently
    truncated scan is indistinguishable from a complete one.

    Only reached when ``decompose`` is set: the exact-match path needs no scan at all,
    which is why it stays the default.
    """
    from metaedit.domain.browse_filters import MAX_SCAN_ITEMS, SCAN_PAGE_SIZE
    from metaedit.service.planning import ITEM_KIND_BY_QUERY

    item_kind = ITEM_KIND_BY_QUERY.get(selection.kind)
    if item_kind is None:
        raise ValidationError(
            f"unknown selection kind {selection.kind!r}; expected one of "
            f"{sorted(ITEM_KIND_BY_QUERY)}"
        )

    facets = wanted.jellyfin_params()
    matched: list[Any] = []
    scanned = 0
    excluded = 0
    truncated = True
    while scanned < MAX_SCAN_ITEMS and len(matched) < selection.limit:
        page = await client.items(
            kind=item_kind,
            parent_id=selection.parent_id,
            search_term=selection.search,
            start_index=scanned,
            limit=min(SCAN_PAGE_SIZE, MAX_SCAN_ITEMS - scanned),
            filters=facets,
        )
        if not page.Items:
            truncated = False
            break
        for dto in page.Items:
            scanned += 1
            if not wanted.carries(dto):
                excluded += 1
                continue
            if _carries(dto, selection):
                matched.append(dto)
                if len(matched) >= selection.limit:
                    break
        if len(matched) >= selection.limit:
            # The selection is full; more matching items exist than were asked for, which
            # is the requested limit being honoured rather than an incomplete read.
            truncated = False
            break
        if scanned >= (page.TotalRecordCount or 0):
            truncated = False
            break
    return SelectionOutcome(
        items=matched[: selection.limit],
        scanned=scanned,
        excluded=excluded,
        truncated=truncated,
    )


def _carries(dto: Any, selection: RemovalSelection) -> bool:
    """Whether a DTO actually holds the target in one of the requested fields."""
    from metaedit.domain.genre_removal import matches

    for name in selection.fields:
        values = getattr(dto, name, None) or []
        if any(matches(value, selection.target) for value in values):
            return True
        if selection.decompose:
            from metaedit.domain.genre_removal import split_packed_values

            if any(
                matches(piece, selection.target)
                for value in values
                for piece in split_packed_values(value)
            ):
                return True
    return False


async def build_job(
    *,
    client: JellyfinClient,
    selection: RemovalSelection,
) -> RemovalJob:
    """Diff every matching item, recording which would change. Writes nothing."""
    target = _validate_target(selection.target)
    fields = _fields_for(selection.fields)

    outcome = await find_items(client, selection=selection)
    job = RemovalJob(
        job_id=uuid.uuid4().hex,
        batch_id=uuid.uuid4().hex,
        created_at=datetime.now(UTC),
        requested=selection.as_dict(),
        report=outcome,
    )

    for dto in outcome.items:
        try:
            kind = item_kind_for(dto)
            item = from_dto(dto.model_dump(), kind)
        except Exception as exc:
            # A DTO shape we cannot normalise is one item's problem. Raising would abandon
            # the run with no record of which item was at fault.
            job.items.append(
                BulkItem(
                    item_id=getattr(dto, "Id", "") or "",
                    name=getattr(dto, "Name", "") or "",
                    kind=getattr(dto, "Type", "") or "",
                    plan=_placeholder_plan(dto),
                    skipped_reason=f"could not read this item: {type(exc).__name__}",
                )
            )
            continue

        plan, outcomes = plan_for_item(
            item, target=target, fields=fields, decompose=selection.decompose
        )
        job.outcomes[item.item_id] = outcomes
        job.items.append(
            BulkItem(
                item_id=item.item_id,
                name=item.name,
                kind=item.kind,
                plan=plan,
                skipped_reason=None if plan.proposed else f"does not carry {target!r}",
            )
        )

    _removal_registry.add(job)
    log.info("removal_job_built", **job.summary())
    return job


async def stream_job_diff(job: RemovalJob) -> AsyncIterator[dict[str, Any]]:
    """Yield the reviewed plan for each item, one event at a time."""
    for index, item in enumerate(job.items):
        yield {
            "type": "item",
            "index": index,
            "item_id": item.item_id,
            "name": item.name,
            "kind": item.kind,
            "skipped_reason": item.skipped_reason,
            "applicable": item.applicable,
            "diff": item.plan.as_dict(),
            "removals": [outcome.as_dict() for outcome in job.outcomes.get(item.item_id, [])],
        }
    yield {"type": "summary", **job.summary(), "emptied": job.emptied}


def _placeholder_plan(dto: Any) -> DiffPlan:
    """A plan-shaped placeholder for an unreadable item, so the event shape is stable."""
    from metaedit.domain.writable import ItemKind

    kind: ItemKind = (
        dto.Type if dto.Type in ("MusicArtist", "MusicAlbum", "Audio") else "MusicArtist"
    )
    return DiffPlan(
        item_id=getattr(dto, "Id", "") or "",
        kind=kind,
        item_name=getattr(dto, "Name", "") or "",
        candidate=Candidate(kind=kind, name=getattr(dto, "Name", "") or ""),
        confidence=_operator_confidence(),
        mapping=MappingResult(),
    )


async def apply_removal_job(
    *,
    session: AsyncSession,
    client: JellyfinClient,
    job: RemovalJob,
    selections: dict[str, list[str]] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Apply a reviewed removal, isolating failures per item.

    Delegates to ``bulk.apply_job`` so there is exactly one write path, one snapshotted
    write, and one batch-revert mechanism for both kinds of batch. The events are the
    same shape either way, so the endpoint streams them verbatim.
    """
    from metaedit.service.bulk import apply_job

    async for event in apply_job(
        session=session,
        client=client,
        job=job,
        selections=selections,
    ):
        yield event


async def get_removal_job_or_404(job_id: str) -> RemovalJob:
    job = _removal_registry.get(job_id)
    if job is None:
        raise NotFoundError(
            f"Unknown or expired removal job {job_id!r}. Jobs are held in memory and a "
            "restart clears them; run the diff again."
        )
    return job


__all__ = [
    "RemovalJob",
    "RemovalSelection",
    "apply_removal_job",
    "build_job",
    "find_items",
    "get_removal_job_or_404",
    "plan_for_item",
    "removal_registry",
    "stream_job_diff",
]
