"""Diff: choosing changes, and turning them into a write payload.

This is the last pure step before a write. It takes the mapping's proposals and the
user's selection, and produces the exact body that will be sent.

The safety property it exists to enforce is the one in ADR 0003: ``POST /items/{id}``
is a *full overwrite*, so the body must contain every writable field. Anything not
selected is carried through at its current value by ``snapshot.to_payload`` — which
is why selection is expressed as "these fields changed" and never as "this is the
body".

Two rules are enforced here rather than trusted to callers:

* **Only planned fields may be selected.** A request naming any other field is
  rejected, because on a full-overwrite API a field the caller can name but we did
  not plan is a field they can silently destroy.
* **A selection must actually change something.** Selecting a key whose value is
  identical to what is already there is accepted (it is harmless) but reported, so a
  caller can tell "nothing to do" from "wrote something".
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from metaedit.domain.confidence import Confidence
from metaedit.domain.mapping import Candidate, FieldChange, MappingResult
from metaedit.domain.snapshot import NormalizedItem, to_payload
from metaedit.domain.writable import ItemKind, payload_field_set

Summary = dict[str, Any]


class SelectionError(ValueError):
    """A selection names something the plan does not permit."""


@dataclass(frozen=True, slots=True)
class DiffPlan:
    """Everything known about one proposed edit, ready for review."""

    item_id: str
    kind: ItemKind
    item_name: str
    candidate: Candidate
    confidence: Confidence
    mapping: MappingResult
    # The item's version token at read time, for optimistic concurrency (ADR 0007).
    etag: str | None = None
    locked_fields: tuple[str, ...] = ()

    @property
    def proposed(self) -> list[FieldChange]:
        """Changes worth applying: they differ and no rule withheld them."""
        return [change for change in self.mapping.changes if change.changes_anything]

    @property
    def selectable_fields(self) -> frozenset[str]:
        return frozenset(change.field for change in self.proposed)

    @property
    def default_selection(self) -> list[str]:
        """What the confidence verdict allows to be pre-selected.

        An empty list is the safe outcome, not a failure: it means the match needs
        review, so nothing is written until a human chooses.
        """
        if not self.confidence.accepts_by_default:
            return []
        return [change.field for change in self.proposed if change.selected]

    def as_dict(self) -> Summary:
        return {
            "item_id": self.item_id,
            "kind": self.kind,
            "name": self.item_name,
            "etag": self.etag,
            "locked_fields": list(self.locked_fields),
            "confidence": {
                "total": self.confidence.total,
                "verdict": self.confidence.verdict,
                "accepts_by_default": self.confidence.accepts_by_default,
                "mbid_conflict": self.confidence.mbid_conflict,
                "components": [
                    {
                        "name": component.name,
                        "value": component.value,
                        "applied_weight": round(component.applied_weight, 4),
                        "detail": component.detail,
                    }
                    for component in self.confidence.components
                ],
                "notes": self.confidence.notes,
            },
            "candidate": {
                "name": self.candidate.name,
                "mbid": self.candidate.mbid,
                "artist": self.candidate.artist,
                "url": self.candidate.url,
                "year": self.candidate.year,
                "response_id": self.candidate.response_id,
            },
            "changes": [_change_dict(change) for change in self.mapping.changes],
            "withheld": [_change_dict(change) for change in self.mapping.skipped],
            "default_selection": self.default_selection,
        }

    # ------------------------------------------------------------- selection

    def resolve_selection(self, requested: Iterable[str] | None) -> list[FieldChange]:
        """Validate a requested field set against what this plan actually allows.

        ``None`` means "use the default selection", which is what the UI sends when
        the operator accepts the pre-ticked set.
        """
        if requested is None:
            wanted = set(self.default_selection)
        else:
            wanted = set(requested)
            unknown = wanted - self.selectable_fields
            if unknown:
                permitted = ", ".join(sorted(self.selectable_fields)) or "none"
                raise SelectionError(
                    f"these fields are not proposed for this item: {', '.join(sorted(unknown))}. "
                    f"Permitted: {permitted}"
                )

        locked = set(self.locked_fields)
        if locked & wanted:
            raise SelectionError(
                "these fields are locked in Jellyfin and must be unlocked there first: "
                f"{', '.join(sorted(locked & wanted))}"
            )
        return [change for change in self.proposed if change.field in wanted]

    def build_payload(
        self, base: NormalizedItem, requested: Iterable[str] | None = None
    ) -> dict[str, Any]:
        """The complete body to send, with only the selected fields changed."""
        selected = self.resolve_selection(requested)
        return to_payload(base, self.accepted_values(selected))

    def accepted_values(self, selected: Iterable[FieldChange]) -> dict[str, Any]:
        """The field -> new value mapping for the selected changes.

        ``to_payload`` fills every other key from ``base``, so a field absent from
        this dict is *preserved*, not cleared. That is the whole point: clearing is
        something a caller must ask for explicitly by proposing an empty value.
        """
        return {change.field: change.proposed for change in selected}

    def summary(self, selected: Iterable[FieldChange]) -> Summary:
        chosen = list(selected)
        changed = [change for change in chosen if change.changes_anything]
        unchanged = [change for change in chosen if not change.changes_anything]
        return {
            "selected": [change.field for change in chosen],
            "changed": [change.field for change in changed],
            "unchanged": [change.field for change in unchanged],
            "changed_count": len(changed),
            "nothing_to_do": not changed,
            "provenance": {
                "response_id": self.candidate.response_id,
                "request_ids": list(self.candidate.request_ids),
            },
        }


def _change_dict(change: FieldChange) -> Summary:
    return {
        "field": change.field,
        "current": change.current,
        "proposed": change.proposed,
        "mode": change.mode,
        "reason": change.reason,
        "source": change.source,
        "selected": change.selected,
        "changes_anything": change.changes_anything,
        "withheld_reason": change.withheld_reason,
        "provenance": change.provenance,
    }


def build_plan(
    *,
    item: NormalizedItem,
    candidate: Candidate,
    confidence: Confidence,
    mapping: MappingResult,
    locked_fields: Iterable[str] = (),
) -> DiffPlan:
    """Assemble a plan from its parts.

    ``locked_fields`` is honoured here rather than in the mapping because Jellyfin's
    ``LockedFields`` is a statement about what may be written, not about what
    Last.fm offers: a locked field is still worth *showing* as a proposal, with the
    reason it cannot be applied.
    """
    return DiffPlan(
        item_id=item.item_id,
        kind=item.kind,
        item_name=item.name,
        candidate=candidate,
        confidence=confidence,
        mapping=mapping,
        etag=item.etag,
        locked_fields=tuple(sorted(locked_fields)),
    )


def assert_payload_is_safe(payload: dict[str, Any], kind: ItemKind) -> None:
    """Belt-and-braces check that a payload cannot null an unintended field.

    ``to_payload`` already enforces this, so this exists to be called at the boundary
    where the payload leaves our code. If it ever fires, something built a body
    without going through ``to_payload``, which is exactly the mistake ADR 0003
    exists to make impossible.
    """
    expected = payload_field_set(kind)
    actual = set(payload)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        msg = (
            "refusing to send a payload that is not exactly the writable field set: "
            f"kind={kind} missing={missing} extra={extra}"
        )
        raise SelectionError(msg)
