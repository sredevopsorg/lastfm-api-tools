"""Applying a reviewed change set to Jellyfin.

This is the only code path in the project that writes to Jellyfin, and it implements
the ten-step rule from the plan's §5.2. Two properties make it safe rather than
merely working:

* **The payload is always complete.** ``snapshot.to_payload`` emits exactly the
  writable field set, so a field nobody selected is carried through at its current
  value rather than nulled. That is the difference between editing an item and
  destroying most of it (ADR 0003).
* **The snapshot is written before the item is.** If the write half-fails, or a
  later mapping turns out to be wrong, the previous values are already durable and
  the change is reversible in one call (ADR 0004).

Ordering within the transaction matters and is deliberate: snapshot, then write, then
audit, then commit. A snapshot that exists without a corresponding write is harmless
history; a write that exists without a snapshot is an unrecoverable edit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from metaedit.adapters.jellyfin.client import JellyfinClient
from metaedit.db.models import AuditLog, Snapshot
from metaedit.domain.diff import DiffPlan, assert_payload_is_safe
from metaedit.domain.errors import ConflictError, UpstreamError
from metaedit.domain.mapping import Candidate
from metaedit.domain.snapshot import (
    from_dto,
    from_snapshot_row,
    snapshot_fields,
    to_payload,
)
from metaedit.domain.writable import ItemKind
from metaedit.logging import get_logger

log = get_logger(__name__)


@dataclass(slots=True)
class ApplyOutcome:
    """What happened, in enough detail to show and to undo."""

    item_id: str
    kind: ItemKind
    applied: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    snapshot_id: int | None = None
    etag_before: str | None = None
    etag_after: str | None = None
    # The authoritative state after the write, re-read from the server rather than
    # assumed from the payload we sent.
    state: dict[str, Any] | None = None
    duration_ms: int = 0
    # Present on every outcome, null where the caller supplied none. A field that is
    # sometimes absent forces consumers to handle two shapes for one concept.
    idempotency_key: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "kind": self.kind,
            "applied": self.applied,
            "unchanged": self.unchanged,
            "snapshot_id": self.snapshot_id,
            "etag_before": self.etag_before,
            "etag_after": self.etag_after,
            "duration_ms": self.duration_ms,
            "state": self.state,
            "idempotency_key": self.idempotency_key,
        }


async def apply_plan(
    *,
    session: AsyncSession,
    client: JellyfinClient,
    plan: DiffPlan,
    requested: list[str] | None,
    expected_etag: str | None = None,
    user_id: str | None = None,
) -> ApplyOutcome:
    """Apply a reviewed plan, snapshotting first.

    Raises ``ConflictError`` when the item changed since it was read, so a stale
    overwrite is refused rather than silently reverting someone else's edit.
    """
    started = datetime.now(UTC)

    # 1-2. Re-read so the payload is built from current server state, not from
    # whatever the plan was assembled against.
    dto = await client.item(plan.item_id, user_id=user_id)
    current = from_dto(dto.model_dump(), plan.kind)

    # 3-4. Validate the selection against what the plan permits.
    selected = plan.resolve_selection(requested)

    # 5. Check the version token before anything is written.
    _assert_etag_matches(expected=expected_etag or plan.etag, actual=current.etag)

    # 6. Build the complete body.
    payload = to_payload(current, plan.accepted_values(selected))
    assert_payload_is_safe(payload, plan.kind)

    changed = [change.field for change in selected if change.changes_anything]
    unchanged = [change.field for change in selected if not change.changes_anything]

    # 7. Persist the snapshot BEFORE the write. If the write fails afterwards, an
    # orphaned snapshot is harmless; the reverse is not recoverable.
    snapshot = Snapshot(
        item_id=plan.item_id,
        kind=plan.kind,
        name=current.name,
        fields=snapshot_fields(current),
        etag=current.etag,
        date_last_saved=current.date_last_saved,
        source_op="apply",
    )
    session.add(snapshot)
    await session.flush()

    # 8. Write.
    try:
        await client.update_item(plan.item_id, payload)
    except UpstreamError as exc:
        await _record_audit(
            session,
            plan=plan,
            payload=payload,
            changed=changed,
            outcome="failed",
            error_code=exc.code,
            duration_ms=_elapsed_ms(started),
        )
        await session.commit()
        raise

    # 9. Re-read: the server is authoritative, and we should report what it says
    # rather than what we hoped we sent.
    refreshed_dto = await client.item(plan.item_id, user_id=user_id)
    refreshed = from_dto(refreshed_dto.model_dump(), plan.kind)

    # 10. Record the outcome alongside its provenance.
    await _record_audit(
        session,
        plan=plan,
        payload=payload,
        changed=changed,
        outcome="applied",
        error_code=None,
        duration_ms=_elapsed_ms(started),
    )
    await session.commit()

    log.info(
        "metadata_applied",
        item_id=plan.item_id,
        kind=plan.kind,
        changed=changed,
        snapshot_id=snapshot.id,
    )

    return ApplyOutcome(
        item_id=plan.item_id,
        kind=plan.kind,
        applied=changed,
        unchanged=unchanged,
        snapshot_id=int(snapshot.id),
        etag_before=current.etag,
        etag_after=refreshed.etag,
        state=refreshed.as_state(),
        duration_ms=_elapsed_ms(started),
    )


async def revert_snapshot(
    *,
    session: AsyncSession,
    client: JellyfinClient,
    snapshot: Snapshot,
    user_id: str | None = None,
) -> ApplyOutcome:
    """Restore the values captured by a snapshot.

    A revert is an ordinary write, not a special case: it builds the same complete
    payload from the snapshot's fields and writes it. That keeps one code path for
    writing, so the completeness guarantee cannot hold on apply and quietly not on
    revert.

    The revert itself is snapshotted, so undoing an undo works and no history is ever
    mutated.
    """
    started = datetime.now(UTC)
    kind: ItemKind = snapshot.kind  # type: ignore[assignment]

    dto = await client.item(snapshot.item_id, user_id=user_id)
    current = from_dto(dto.model_dump(), kind)
    restored = from_snapshot_row(
        {
            "kind": kind,
            "item_id": snapshot.item_id,
            "name": snapshot.name,
            "etag": snapshot.etag,
            "date_last_saved": snapshot.date_last_saved,
            "fields": snapshot.fields,
        }
    )

    # Every writable field from the snapshot, with everything else carried through
    # from the current state.
    target = {**snapshot_fields(restored)}
    payload = to_payload(current, target)
    assert_payload_is_safe(payload, kind)

    changed = [name for name in target if target[name] != current.get(name)]
    unchanged = [name for name in target if target[name] == current.get(name)]

    new_snapshot = Snapshot(
        item_id=snapshot.item_id,
        kind=kind,
        name=current.name,
        fields=snapshot_fields(current),
        etag=current.etag,
        date_last_saved=current.date_last_saved,
        source_op="revert",
    )
    session.add(new_snapshot)
    await session.flush()

    try:
        await client.update_item(snapshot.item_id, payload)
    except UpstreamError as exc:
        session.add(
            AuditLog(
                item_id=snapshot.item_id,
                action="revert",
                changed_fields=changed,
                payload=payload,
                outcome="failed",
                error_code=exc.code,
                duration_ms=_elapsed_ms(started),
            )
        )
        await session.commit()
        raise

    refreshed = from_dto((await client.item(snapshot.item_id, user_id=user_id)).model_dump(), kind)
    session.add(
        AuditLog(
            item_id=snapshot.item_id,
            action="revert",
            changed_fields=changed,
            payload=payload,
            lastfm_candidate=None,
            lastfm_request_ids=[],
            outcome="reverted",
            error_code=None,
            duration_ms=_elapsed_ms(started),
        )
    )
    await session.commit()

    log.info("metadata_reverted", item_id=snapshot.item_id, from_snapshot=snapshot.id)
    return ApplyOutcome(
        item_id=snapshot.item_id,
        kind=kind,
        applied=changed,
        unchanged=unchanged,
        snapshot_id=int(new_snapshot.id),
        etag_before=current.etag,
        etag_after=refreshed.etag,
        state=refreshed.as_state(),
        duration_ms=_elapsed_ms(started),
    )


def _assert_etag_matches(*, expected: str | None, actual: str | None) -> None:
    """Optimistic concurrency (ADR 0007).

    Verified live that Jellyfin 12.2 does return an ``Etag`` for music items, so this
    is a real check rather than a hopeful one. It is best-effort by nature -- nothing
    prevents an edit in Jellyfin's own UI between this read and the write -- but it
    narrows the window from "however long a human spent reviewing a diff" to a single
    round trip.
    """
    if expected and actual and expected != actual:
        raise ConflictError(
            "The item changed since this change set was prepared, so applying it would "
            "silently revert that change. Re-read the item and review the diff again.",
            current={"expected_etag": expected, "actual_etag": actual},
        )


async def _record_audit(
    session: AsyncSession,
    *,
    plan: DiffPlan,
    payload: dict[str, Any],
    changed: list[str],
    outcome: str,
    error_code: str | None,
    duration_ms: int,
) -> None:
    """Provenance for every write attempt, successful or not."""
    session.add(
        AuditLog(
            item_id=plan.item_id,
            action="apply",
            changed_fields=changed,
            payload=payload,
            lastfm_candidate={
                "name": plan.candidate.name,
                "mbid": plan.candidate.mbid,
                "response_id": plan.candidate.response_id,
                "confidence": plan.confidence.total,
                "verdict": plan.confidence.verdict,
            },
            # Ties the written values back to the exact archived responses.
            lastfm_request_ids=list(plan.candidate.request_ids),
            outcome=outcome,
            error_code=error_code,
            duration_ms=duration_ms,
        )
    )


def _elapsed_ms(started: datetime) -> int:
    return int((datetime.now(UTC) - started).total_seconds() * 1000)


def candidate_summary(candidate: Candidate) -> dict[str, Any]:
    """A display-safe view of the candidate, for the diff response."""
    return {
        "kind": candidate.kind,
        "name": candidate.name,
        "mbid": candidate.mbid,
        "artist": candidate.artist,
        "year": candidate.year,
        "duration_ms": candidate.duration_ms,
        "url": candidate.url,
        "listeners": candidate.listeners,
        "playcount": candidate.playcount,
        "tag_count": len(candidate.tags),
        "has_overview": bool(candidate.overview),
        "response_id": candidate.response_id,
    }


__all__ = ["ApplyOutcome", "apply_plan", "candidate_summary", "revert_snapshot"]
