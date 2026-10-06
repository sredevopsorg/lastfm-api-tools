"""Mapping: what Last.fm offers, and whether we may write it.

Turns a Last.fm candidate plus the item's current state into a list of proposed
changes, each carrying its current value, its proposed value, the rule that allowed
it, and where it came from.

This is the layer where ADR 0003's hazard actually bites. ``POST /Items/{itemId}``
is a full overwrite, so "the value we send" *becomes* the item's state. A mapping
that proposes a value for a field the user did not intend to touch is therefore a
destructive operation, not a suggestion. Two consequences shape the code:

* **Every field is either explicitly proposed or explicitly not.** Anything not
  planned here is carried through unchanged by ``snapshot.to_payload``, and the
  default mode for a field Last.fm cannot inform is ``keep_existing``.
* **``merge`` is the default for collections.** Last.fm tags are community data
  with no curation behind them, so they add to a curated list rather than replacing
  it. Replacement is available, per field, deliberately.

Modes are named for the *question they answer* rather than the mechanism:
``keep_existing`` ("leave it alone"), ``fill_if_empty`` ("only if it has nothing"),
``replace`` ("mine wins"), ``merge`` ("mine adds to yours").
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Any, Literal

from metaedit.domain.snapshot import NormalizedItem
from metaedit.domain.tags import TagInput, TagOutcome, TagPolicy
from metaedit.domain.text import html_to_text
from metaedit.domain.writable import ItemKind

Mode = Literal["keep_existing", "fill_if_empty", "replace", "merge"]

FieldKind = Literal["text", "list", "dict", "urls", "int"]


@dataclass(frozen=True, slots=True)
class FieldPolicy:
    """The rule governing one writable field.

    ``reason`` is not decoration: it is what the UI shows next to a proposed change,
    and what an operator reads when asking "why did this tool want to change that".
    """

    field: str
    mode: Mode
    field_kind: FieldKind
    source: str
    reason: str
    # Off by default for fields that are defensible but opinionated, so the operator
    # opts in rather than discovering a change they did not expect.
    enabled: bool = True
    # Reject a proposed value that is not worth the write (e.g. a two-word bio).
    minimum_length: int = 0


# The plan's §5.1 defaults. Fields Last.fm cannot inform are absent on purpose:
# absence means keep_existing, which is enforced by `to_payload` carrying the
# current value through rather than by this table listing every field.
DEFAULT_POLICIES: dict[str, FieldPolicy] = {
    "Genres": FieldPolicy(
        field="Genres",
        mode="merge",
        field_kind="list",
        source="lastfm.tags",
        reason="Last.fm's top tags add to the item's existing genres",
    ),
    "Tags": FieldPolicy(
        field="Tags",
        mode="merge",
        field_kind="list",
        source="lastfm.tags",
        reason="remaining Last.fm tags add to the item's existing tags",
    ),
    "Overview": FieldPolicy(
        field="Overview",
        mode="fill_if_empty",
        field_kind="text",
        source="lastfm.bio",
        reason="a Last.fm biography, only when the item has no overview",
        # "British band." is not worth a write.
        minimum_length=40,
    ),
    "ProviderIds": FieldPolicy(
        field="ProviderIds",
        mode="fill_if_empty",
        field_kind="dict",
        source="lastfm.mbid",
        reason="the MusicBrainz id, which enables exact matching next time",
    ),
    "ExternalUrls": FieldPolicy(
        field="ExternalUrls",
        mode="merge",
        field_kind="urls",
        source="lastfm.url",
        reason="a link back to Last.fm, as its terms require wherever its data is shown",
    ),
    "ProductionYear": FieldPolicy(
        field="ProductionYear",
        mode="fill_if_empty",
        field_kind="int",
        source="lastfm.releasedate",
        reason="the year parsed from Last.fm's release date",
        # Release dates disagree between sources by a year routinely, so this is
        # opt-in rather than a default change to an item's year.
        enabled=False,
    ),
    "ForcedSortName": FieldPolicy(
        field="ForcedSortName",
        mode="fill_if_empty",
        field_kind="text",
        source="lastfm.name",
        reason="Last.fm's canonical spelling, used for sorting",
        enabled=False,
    ),
}


@dataclass(frozen=True, slots=True)
class Candidate:
    """A Last.fm entity proposed as the match for an item.

    A plain value object, deliberately free of ORM types and of the archive: the
    caller assembles it from an archived response, so this layer stays pure and
    testable without a database.
    """

    kind: ItemKind
    name: str
    mbid: str | None = None
    artist: str | None = None
    year: int | None = None
    duration_ms: int | None = None
    # Verbatim from Last.fm; sanitised here, not by the caller.
    overview: str | None = None
    overview_truncated: bool = False
    tags: list[TagInput] = dataclass_field(default_factory=list)
    url: str | None = None
    listeners: int | None = None
    playcount: int | None = None
    # Provenance: which archived response justified this, so a written value can be
    # traced back to the data it came from.
    response_id: str | None = None
    request_ids: list[int] = dataclass_field(default_factory=list)

    def identity_provider_key(self) -> str | None:
        """Which ``ProviderIds`` key this candidate's MBID belongs under.

        Jellyfin distinguishes artist, album and track ids, so the same MBID means
        different things depending on what it identifies.
        """
        return {
            "MusicArtist": "MusicBrainzArtist",
            "MusicAlbum": "MusicBrainzAlbum",
            "Audio": "MusicBrainzTrack",
        }.get(self.kind)


@dataclass(frozen=True, slots=True)
class FieldChange:
    """One proposed change, with everything needed to judge and to revert it."""

    field: str
    current: Any
    proposed: Any
    mode: Mode
    reason: str
    source: str
    # Whether the UI should pre-tick this change. False for anything requiring
    # review, so the default is never "apply everything".
    selected: bool = False
    # Why a change was withheld, when it was.
    withheld_reason: str | None = None
    provenance: dict[str, Any] = dataclass_field(default_factory=dict)

    @property
    def is_noop(self) -> bool:
        # bool() because comparing two Any values yields Any, and this is a
        # predicate: a truthy non-bool here would be a latent surprise.
        return bool(self.current == self.proposed)

    @property
    def changes_anything(self) -> bool:
        return not self.is_noop


@dataclass(frozen=True, slots=True)
class MappingResult:
    """Every field we considered, including the ones we decided to leave alone."""

    changes: list[FieldChange] = dataclass_field(default_factory=list)
    skipped: list[FieldChange] = dataclass_field(default_factory=list)
    tag_outcome: TagOutcome | None = None

    @property
    def proposed(self) -> list[FieldChange]:
        return [change for change in self.changes if change.changes_anything]

    def by_field(self, name: str) -> FieldChange | None:
        for change in [*self.changes, *self.skipped]:
            if change.field == name:
                return change
        return None


def build_changes(
    item: NormalizedItem,
    candidate: Candidate,
    *,
    policies: dict[str, FieldPolicy] | None = None,
    tag_policy: TagPolicy | None = None,
    overrides: dict[str, Mode] | None = None,
    default_selected: bool = True,
) -> MappingResult:
    """Propose changes for one item.

    ``default_selected`` is how the confidence verdict reaches this layer: a match
    that needs review passes ``False`` so nothing is pre-ticked, and the operator
    decides. It is a parameter rather than something read from the candidate so the
    decision stays visible at the call site.
    """
    policies = {**DEFAULT_POLICIES, **(policies or {})}
    overrides = overrides or {}
    tag_policy = tag_policy or TagPolicy()

    existing_genres = _as_str_list(item.get("Genres"))
    existing_tags = _as_str_list(item.get("Tags"))
    # Classify only. Merging is per field -- and per field *mode* -- so it happens
    # below, where each field's effective policy is known. Merging here instead would
    # make a field-level "replace" override silently ineffective.
    tag_outcome = tag_policy.classify(candidate.tags)

    changes: list[FieldChange] = []
    skipped: list[FieldChange] = []

    def consider(
        policy: FieldPolicy,
        proposed: Any,
        *,
        detail: str | None = None,
        provenance: dict[str, Any] | None = None,
    ) -> None:
        effective = _effective_policy(policy, overrides)
        current = item.get(policy.field, _missing_for(candidate.kind, policy.field))
        change = FieldChange(
            field=policy.field,
            current=current,
            proposed=proposed,
            mode=effective.mode,
            reason=detail or policy.reason,
            source=policy.source,
            provenance={
                "response_id": candidate.response_id,
                "request_ids": list(candidate.request_ids),
                **(provenance or {}),
            },
        )
        if _withheld(effective, change, proposed):
            skipped.append(
                FieldChange(
                    **{
                        **_as_kwargs(change),
                        "withheld_reason": _withheld(effective, change, proposed),
                    }
                )
            )
            return
        changes.append(
            FieldChange(
                **{
                    **_as_kwargs(change),
                    # A change that would write nothing must not be pre-selected;
                    # "selected" implies the operator would be changing something.
                    "selected": default_selected and change.changes_anything,
                }
            )
        )

    # --- tags -----------------------------------------------------------------
    for field_name, proposed, existing, label in (
        ("Genres", tag_outcome.genres, existing_genres, "genres"),
        ("Tags", tag_outcome.tags, existing_tags, "tags"),
    ):
        policy = policies[field_name]
        if not policy.enabled:
            skipped.append(_disabled(policy, item, candidate))
            continue
        effective = _effective_policy(policy, overrides)
        value = (
            tag_policy.merge_into(proposed, existing)
            if effective.mode == "merge"
            else list(proposed)
        )
        consider(
            policy,
            value,
            detail=f"{_tag_detail(tag_outcome, label)}; {policy.reason}",
        )

    # --- overview -------------------------------------------------------------
    overview_policy = policies["Overview"]
    if overview_policy.enabled:
        cleaned = html_to_text(candidate.overview)
        if cleaned.text is None:
            skipped.append(
                _nothing_to_offer(overview_policy, item, candidate, "Last.fm has no bio")
            )
        else:
            note = overview_policy.reason
            if candidate.overview_truncated or cleaned.truncated_by_lastfm:
                # Say so rather than presenting a truncated bio as complete.
                note = f"{note} (Last.fm truncates its summary)"
            consider(overview_policy, cleaned.text, detail=note)
    else:
        skipped.append(_disabled(overview_policy, item, candidate))

    # --- provider ids ---------------------------------------------------------
    provider_policy = policies["ProviderIds"]
    if provider_policy.enabled and candidate.mbid:
        key = candidate.identity_provider_key()
        if key:
            current_ids = dict(item.get("ProviderIds") or {})
            if current_ids.get(key):
                skipped.append(
                    _withheld_change(
                        provider_policy,
                        item,
                        candidate,
                        f"the item already has {key}",
                    )
                )
            else:
                consider(
                    provider_policy,
                    {**current_ids, key: candidate.mbid},
                    detail=f"adds {key}",
                )
    elif provider_policy.enabled:
        skipped.append(_nothing_to_offer(provider_policy, item, candidate, "Last.fm has no MBID"))

    # --- external urls --------------------------------------------------------
    urls_policy = policies["ExternalUrls"]
    if urls_policy.enabled and candidate.url:
        current_urls = list(item.get("ExternalUrls") or [])
        if any(
            entry.get("Url") == candidate.url for entry in current_urls if isinstance(entry, dict)
        ):
            skipped.append(
                _withheld_change(
                    urls_policy, item, candidate, "the Last.fm link is already present"
                )
            )
        else:
            consider(
                urls_policy,
                [*current_urls, {"Name": "Last.fm", "Url": candidate.url}],
            )
    elif urls_policy.enabled:
        skipped.append(_nothing_to_offer(urls_policy, item, candidate, "Last.fm has no page url"))

    # --- year, sort name (opt-in) --------------------------------------------
    year_policy = policies["ProductionYear"]
    if year_policy.enabled:
        if candidate.year is None:
            skipped.append(_nothing_to_offer(year_policy, item, candidate, "no release date"))
        else:
            consider(year_policy, candidate.year)
    else:
        skipped.append(_disabled(year_policy, item, candidate))

    sort_policy = policies["ForcedSortName"]
    if sort_policy.enabled:
        consider(sort_policy, candidate.name)
    else:
        skipped.append(_disabled(sort_policy, item, candidate))

    return MappingResult(changes=changes, skipped=skipped, tag_outcome=tag_outcome)


# --------------------------------------------------------------------- helpers


def _effective_policy(policy: FieldPolicy, overrides: dict[str, Mode]) -> FieldPolicy:
    override = overrides.get(policy.field)
    if override is None or override == policy.mode:
        return policy
    return FieldPolicy(
        field=policy.field,
        mode=override,
        field_kind=policy.field_kind,
        source=policy.source,
        reason=policy.reason,
        enabled=policy.enabled,
        minimum_length=policy.minimum_length,
    )


def _withheld(policy: FieldPolicy, change: FieldChange, proposed: Any) -> str | None:
    """Why this proposal must not be applied, or ``None`` if it may be.

    Note the ordering: mode is checked before emptiness, so ``replace`` genuinely
    replaces even with an empty value, while ``fill_if_empty`` and ``merge`` leave a
    populated field alone.
    """
    if _is_absent(proposed):
        return "nothing to write"
    if policy.mode == "keep_existing":
        return "the policy is to leave this field alone"
    if policy.mode == "fill_if_empty" and not _is_absent(change.current):
        return "the item already has a value"
    if policy.minimum_length and len(str(proposed).strip()) < policy.minimum_length:
        return f"shorter than {policy.minimum_length} characters"
    return None


def _as_kwargs(change: FieldChange) -> dict[str, Any]:
    return {
        "field": change.field,
        "current": change.current,
        "proposed": change.proposed,
        "mode": change.mode,
        "reason": change.reason,
        "source": change.source,
        "selected": change.selected,
        "provenance": change.provenance,
    }


def _withheld_change(
    policy: FieldPolicy, item: NormalizedItem, candidate: Candidate, reason: str
) -> FieldChange:
    return FieldChange(
        field=policy.field,
        current=item.get(policy.field),
        proposed=None,
        mode=policy.mode,
        reason=policy.reason,
        source=policy.source,
        withheld_reason=reason,
        provenance={"response_id": candidate.response_id},
    )


def _nothing_to_offer(
    policy: FieldPolicy, item: NormalizedItem, candidate: Candidate, why: str
) -> FieldChange:
    return _withheld_change(policy, item, candidate, why)


def _disabled(policy: FieldPolicy, item: NormalizedItem, candidate: Candidate) -> FieldChange:
    return _withheld_change(
        policy, item, candidate, "not enabled by default; enable it to use this field"
    )


def _tag_detail(outcome: TagOutcome, which: str) -> str:
    count = len(outcome.genres) if which == "genres" else len(outcome.tags)
    parts = [f"{count} from Last.fm"]
    if outcome.dropped:
        reasons: dict[str, int] = {}
        for drop in outcome.dropped:
            reasons[drop.reason] = reasons.get(drop.reason, 0) + 1
        summary = ", ".join(f"{number} {reason}" for reason, number in sorted(reasons.items()))
        parts.append(f"{len(outcome.dropped)} dropped ({summary})")
    if outcome.overflow and which == "tags":
        parts.append(f"{len(outcome.overflow)} beyond the limit")
    return "; ".join(parts)


def _as_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _is_absent(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, dict)):
        return not value
    return False


def _missing_for(kind: ItemKind, field_name: str) -> Any:
    """The empty value for a field, so `current` is never a bare ``None`` mystery."""
    from metaedit.domain.snapshot import empty_value

    del kind
    return empty_value(field_name)
