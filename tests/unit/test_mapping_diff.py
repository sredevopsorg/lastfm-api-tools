"""Mapping and diff: from a Last.fm candidate to the exact body we would send.

The tests concentrate on what must *not* happen, because ``POST /Items/{itemId}`` is
a full overwrite: a change we propose by accident is a change that becomes the
item's state. The three properties that matter most are that a curated value is
never silently replaced, that a field we did not plan can never be selected, and
that the payload is always the complete writable field set.
"""

from __future__ import annotations

from typing import Any

import pytest

from metaedit.domain.confidence import (
    CandidateIdentity,
    Confidence,
    MatchContext,
    score,
)
from metaedit.domain.diff import (
    DiffPlan,
    SelectionError,
    assert_payload_is_safe,
    build_plan,
)
from metaedit.domain.mapping import (
    DEFAULT_POLICIES,
    Candidate,
    FieldPolicy,
    build_changes,
)
from metaedit.domain.snapshot import NormalizedItem, from_dto
from metaedit.domain.tags import TagInput, TagPolicy
from metaedit.domain.writable import payload_field_set


def dto(kind: str = "MusicArtist", **overrides: Any) -> dict[str, Any]:
    """A realistic-enough Jellyfin item payload."""
    base: dict[str, Any] = {
        "Id": "item-1",
        "Type": kind,
        "Name": "Radiohead",
        "Etag": "etag-1",
        "SourceType": "Library",
        "Genres": [],
        "Tags": [],
        "ProviderIds": {},
        "ExternalUrls": [],
        "LockedFields": [],
        "Overview": "",
        "People": [],
        "Studios": [],
        "ProductionLocations": [],
        "DateLastSaved": "2026-01-01T00:00:00Z",
    }
    base.update(overrides)
    return base


def item(kind: str = "MusicArtist", **overrides: Any) -> NormalizedItem:
    return from_dto(dto(kind, **overrides), kind)  # type: ignore[arg-type]


BIO = "Radiohead are an English rock band formed in 1985 in Abingdon, Oxfordshire."


def candidate(**overrides: Any) -> Candidate:
    base: dict[str, Any] = {
        "kind": "MusicArtist",
        "name": "Radiohead",
        "mbid": "mbid-1",
        "overview": BIO,
        "tags": [TagInput("art rock", 100), TagInput("electronic", 80)],
        "url": "https://www.last.fm/music/Radiohead",
        "response_id": "response-1",
    }
    base.update(overrides)
    return Candidate(**base)


def confident(kind: str = "MusicArtist", **kwargs: Any) -> Confidence:
    context = MatchContext(kind=kind, name="Radiohead", mbid="mbid-1", **kwargs)  # type: ignore[arg-type]
    return score(context, CandidateIdentity("Radiohead", mbid="mbid-1"))


def plan_for(
    *,
    the_item: NormalizedItem | None = None,
    the_candidate: Candidate | None = None,
    confidence: Confidence | None = None,
    tag_policy: TagPolicy | None = None,
    overrides: dict[str, Any] | None = None,
    policies: dict[str, FieldPolicy] | None = None,
    default_selected: bool = True,
    locked_fields: tuple[str, ...] = (),
) -> DiffPlan:
    the_item = the_item or item()
    the_candidate = the_candidate or candidate()
    mapping = build_changes(
        the_item,
        the_candidate,
        tag_policy=tag_policy,
        overrides=overrides,
        policies=policies,
        default_selected=default_selected,
    )
    return build_plan(
        item=the_item,
        candidate=the_candidate,
        confidence=confidence or confident(),
        mapping=mapping,
        locked_fields=locked_fields,
    )


# ------------------------------------------------------------------ defaults


def test_every_default_policy_field_is_a_real_writable_field() -> None:
    """A policy naming a field that does not exist would be silently ignored."""
    writable = payload_field_set("MusicArtist") | payload_field_set("Audio")
    unknown = sorted(set(DEFAULT_POLICIES) - writable)
    assert not unknown, f"policies name fields that are not writable: {unknown}"


def test_no_default_policy_targets_a_field_we_must_not_write() -> None:
    assert not set(DEFAULT_POLICIES) & {"Id", "Etag", "Type", "SourceType", "Path"}


def test_opinionated_fields_are_disabled_by_default() -> None:
    """A year or a sort name is defensible but not something to change unasked."""
    assert DEFAULT_POLICIES["ProductionYear"].enabled is False
    assert DEFAULT_POLICIES["ForcedSortName"].enabled is False
    assert DEFAULT_POLICIES["Genres"].enabled is True


# -------------------------------------------------------------------- genres


def test_genres_merge_with_existing_by_default() -> None:
    result = build_changes(
        item(Genres=["Rock"]),
        candidate(tags=[TagInput("art rock", 100)]),
    )
    assert result.by_field("Genres").proposed == ["Rock", "art rock"]  # type: ignore[union-attr]


def test_replace_mode_discards_existing_genres() -> None:
    result = build_changes(
        item(Genres=["Rock"]),
        candidate(tags=[TagInput("art rock", 100)]),
        overrides={"Genres": "replace"},
    )
    assert result.by_field("Genres").proposed == ["art rock"]  # type: ignore[union-attr]


def test_fill_if_empty_leaves_a_populated_field_alone() -> None:
    result = build_changes(
        item(Genres=["Rock"]),
        candidate(tags=[TagInput("art rock", 100)]),
        overrides={"Genres": "fill_if_empty"},
    )
    withheld = result.by_field("Genres")
    assert withheld is not None
    assert withheld.proposed is None or withheld.withheld_reason
    assert "already has a value" in (withheld.withheld_reason or "")


def test_a_tag_the_blacklist_rejects_never_reaches_the_proposal() -> None:
    result = build_changes(
        item(),
        candidate(tags=[TagInput("seen live", 999), TagInput("art rock", 10)]),
    )
    proposed = result.by_field("Genres").proposed  # type: ignore[union-attr]
    assert proposed == ["art rock"]
    assert "seen live" not in proposed


def test_the_tag_outcome_is_reported_for_explanation() -> None:
    result = build_changes(item(), candidate(tags=[TagInput("seen live", 5)]))
    assert result.tag_outcome is not None
    assert result.tag_outcome.dropped[0].reason == "on the blacklist"


# ------------------------------------------------------------------ overview


def test_overview_fills_an_empty_field() -> None:
    result = build_changes(item(Overview=""), candidate(overview=BIO))
    assert result.by_field("Overview").proposed == BIO  # type: ignore[union-attr]


def test_overview_never_overwrites_a_curated_value_by_default() -> None:
    """The curated overview is the single most expensive thing to lose here."""
    result = build_changes(
        item(Overview="My own careful words about this band."), candidate(overview=BIO)
    )
    change = result.by_field("Overview")
    assert change is not None
    assert change.withheld_reason == "the item already has a value"


def test_overview_can_be_replaced_when_asked() -> None:
    result = build_changes(
        item(Overview="Mine."),
        candidate(overview=BIO),
        overrides={"Overview": "replace"},
    )
    assert result.by_field("Overview").proposed == BIO  # type: ignore[union-attr]


def test_html_in_a_bio_is_stripped_before_it_is_proposed() -> None:
    result = build_changes(
        item(Overview=""),
        candidate(overview=f'<p>{BIO}</p><a href="x">Read more on Last.fm</a>'),
    )
    proposed = result.by_field("Overview").proposed  # type: ignore[union-attr]
    assert "<p>" not in proposed
    assert "read more" not in proposed.lower()


def test_a_thin_bio_is_not_worth_a_write() -> None:
    result = build_changes(item(Overview=""), candidate(overview="British band."))
    change = result.by_field("Overview")
    assert change is not None
    assert "shorter than" in (change.withheld_reason or "")


def test_a_truncated_bio_is_proposed_but_says_so() -> None:
    result = build_changes(item(Overview=""), candidate(overview=BIO, overview_truncated=True))
    change = result.by_field("Overview")
    assert change is not None
    assert "truncates" in change.reason


# --------------------------------------------------------------- provider ids


def test_mbid_is_added_under_the_key_for_the_media_type() -> None:
    for kind, key in (
        ("MusicArtist", "MusicBrainzArtist"),
        ("MusicAlbum", "MusicBrainzAlbum"),
        ("Audio", "MusicBrainzTrack"),
    ):
        result = build_changes(item(kind), candidate(kind=kind, mbid="x"))  # type: ignore[arg-type]
        assert result.by_field("ProviderIds").proposed == {key: "x"}  # type: ignore[union-attr]


def test_adding_an_mbid_keeps_the_other_provider_ids() -> None:
    """ProviderIds is a map: replacing it would drop ids we did not look at."""
    result = build_changes(
        item(ProviderIds={"MusicBrainzReleaseGroup": "rg-1", "Other": "keep-me"}),
        candidate(mbid="artist-1"),
    )
    proposed = result.by_field("ProviderIds").proposed  # type: ignore[union-attr]
    assert proposed == {
        "MusicBrainzReleaseGroup": "rg-1",
        "Other": "keep-me",
        "MusicBrainzArtist": "artist-1",
    }


def test_an_existing_mbid_is_not_overwritten() -> None:
    result = build_changes(
        item(ProviderIds={"MusicBrainzArtist": "already"}), candidate(mbid="different")
    )
    change = result.by_field("ProviderIds")
    assert change is not None
    assert "already has" in (change.withheld_reason or "")


def test_a_candidate_without_an_mbid_offers_nothing() -> None:
    result = build_changes(item(), candidate(mbid=None))
    change = result.by_field("ProviderIds")
    assert change is not None
    assert change.withheld_reason == "Last.fm has no MBID"


# -------------------------------------------------------------- external urls


def test_the_lastfm_link_is_appended() -> None:
    result = build_changes(item(ExternalUrls=[]), candidate())
    proposed = result.by_field("ExternalUrls").proposed  # type: ignore[union-attr]
    assert proposed == [{"Name": "Last.fm", "Url": "https://www.last.fm/music/Radiohead"}]


def test_existing_external_urls_are_preserved() -> None:
    result = build_changes(
        item(ExternalUrls=[{"Name": "Wikipedia", "Url": "https://en.wikipedia.org/x"}]),
        candidate(),
    )
    proposed = result.by_field("ExternalUrls").proposed  # type: ignore[union-attr]
    assert proposed[0]["Name"] == "Wikipedia"
    assert proposed[-1]["Name"] == "Last.fm"


def test_a_duplicate_lastfm_link_is_not_added_twice() -> None:
    url = "https://www.last.fm/music/Radiohead"
    result = build_changes(item(ExternalUrls=[{"Name": "Last.fm", "Url": url}]), candidate())
    change = result.by_field("ExternalUrls")
    assert change is not None
    assert "already present" in (change.withheld_reason or "")


# ---------------------------------------------------------------- opt-in fields


def test_an_opt_in_field_can_be_enabled_explicitly() -> None:
    policies = {
        "ProductionYear": FieldPolicy(
            field="ProductionYear",
            mode="fill_if_empty",
            field_kind="int",
            source="lastfm.releasedate",
            reason="year",
            enabled=True,
        )
    }
    result = build_changes(
        item(kind="MusicAlbum", ProductionYear=None),
        candidate(kind="MusicAlbum", year=1997),
        policies=policies,
    )
    assert result.by_field("ProductionYear").proposed == 1997  # type: ignore[union-attr]


def test_a_disabled_field_is_reported_not_merely_absent() -> None:
    result = build_changes(
        item(kind="MusicAlbum", ProductionYear=None), candidate(kind="MusicAlbum", year=1997)
    )
    change = result.by_field("ProductionYear")
    assert change is not None
    assert "not enabled by default" in (change.withheld_reason or "")


def test_a_field_lastfm_cannot_inform_is_withheld_without_a_reason_noise() -> None:
    """Name is never proposed: Last.fm's spelling is not automatically better."""
    result = build_changes(item(), candidate(name="radiohead"))
    assert "Name" not in {change.field for change in result.changes}


# ------------------------------------------------------------------------ diff


def test_the_payload_is_always_the_complete_writable_field_set() -> None:
    plan = plan_for()
    payload = plan.build_payload(item(), None)
    assert set(payload) == payload_field_set("MusicArtist")
    assert_payload_is_safe(payload, "MusicArtist")


@pytest.mark.parametrize("kind", ["MusicArtist", "MusicAlbum", "Audio"])
def test_payload_completeness_holds_for_every_kind(kind: str) -> None:
    plan = plan_for(
        the_item=item(kind), the_candidate=candidate(kind=kind), confidence=confident(kind)
    )
    payload = plan.build_payload(item(kind), None)
    assert set(payload) == payload_field_set(kind)  # type: ignore[arg-type]


def test_unselected_fields_keep_their_current_values() -> None:
    """The property that stops this tool destroying everything it does not touch."""
    original = item(Overview="Curated.", Genres=["Rock"], ProviderIds={"Other": "keep"})
    plan = plan_for(the_item=original, the_candidate=candidate())
    payload = plan.build_payload(original, ["Genres"])
    assert payload["Overview"] == "Curated.", "an unselected field must be carried through"
    assert payload["ProviderIds"] == {"Other": "keep"}


def test_selection_rejects_a_field_that_was_never_proposed() -> None:
    """On a full-overwrite API, an arbitrary field name is an arbitrary destruction."""
    plan = plan_for()
    with pytest.raises(SelectionError, match="not proposed"):
        plan.resolve_selection(["Name"])


def test_selection_rejects_a_lock_in_jellyfin() -> None:
    plan = plan_for(locked_fields=("Genres",))
    with pytest.raises(SelectionError, match="locked"):
        plan.resolve_selection(["Genres"])


def test_an_empty_selection_produces_the_current_values_unchanged() -> None:
    original = item(Genres=["Rock"], Overview="Mine.")
    plan = plan_for(the_item=original)
    payload = plan.build_payload(original, [])
    assert payload["Genres"] == ["Rock"]
    assert payload["Overview"] == "Mine."
    assert plan.summary([])["nothing_to_do"] is True


def test_the_default_selection_is_empty_when_review_is_needed() -> None:
    """Nothing is pre-ticked for a match a human should look at."""
    review = score(
        MatchContext(kind="MusicArtist", name="Radiohead"),
        CandidateIdentity("Radiohead", mbid="candidate-id"),
    )
    assert review.verdict == "review"
    plan = plan_for(confidence=review, default_selected=False)
    assert plan.default_selection == []


def test_an_mbid_conflict_cannot_be_applied_even_if_requested() -> None:
    """The conflict is a hard stop, not a low score a caller can override."""
    conflict = score(
        MatchContext(kind="MusicArtist", name="Radiohead", mbid="item-id"),
        CandidateIdentity("Radiohead", mbid="other-id"),
    )
    assert conflict.mbid_conflict is True
    plan = plan_for(confidence=conflict, default_selected=False)
    assert plan.as_dict()["confidence"]["mbid_conflict"] is True
    assert plan.default_selection == []


def test_a_considered_but_identical_field_is_reported_as_unchanged() -> None:
    """The distinction the summary exists to make.

    "We would write Genres" is not the same as "Genres was considered and is already
    correct". `plan.proposed` deliberately excludes no-ops, so showing that
    distinction requires the unfiltered change list.
    """
    original = item(Genres=["art rock", "electronic"])
    plan = plan_for(the_item=original, the_candidate=candidate())

    assert "Genres" not in {change.field for change in plan.proposed}, "the merge is a no-op"
    assert "Genres" in {change.field for change in plan.mapping.changes}, "but it was considered"

    summary = plan.summary(plan.mapping.changes)
    assert "Genres" in summary["unchanged"]
    assert "Genres" not in summary["changed"]
    assert summary["nothing_to_do"] is False, "Overview and ProviderIds still change"


def test_a_fully_settled_item_reports_nothing_to_do() -> None:
    """Everything Last.fm offers is already present: the honest answer is no write."""
    settled = item(
        Genres=["art rock", "electronic"],
        Overview=BIO,
        ProviderIds={"MusicBrainzArtist": "mbid-1"},
        ExternalUrls=[{"Name": "Last.fm", "Url": "https://www.last.fm/music/Radiohead"}],
    )
    plan = plan_for(the_item=settled, the_candidate=candidate())
    summary = plan.summary(plan.mapping.changes)
    assert summary["nothing_to_do"] is True
    assert summary["changed"] == []


def test_the_summary_carries_provenance_back_to_the_archived_response() -> None:
    plan = plan_for()
    summary = plan.summary(plan.resolve_selection(None))
    assert summary["provenance"]["response_id"] == "response-1"


def test_every_change_records_its_source_and_reason() -> None:
    plan = plan_for()
    assert plan.proposed, "this scenario must propose something"
    for change in plan.proposed:
        assert change.source
        assert change.reason
        assert change.provenance["response_id"] == "response-1"


def test_the_serialised_plan_separates_proposed_from_withheld() -> None:
    payload = plan_for().as_dict()
    assert payload["changes"]
    assert payload["withheld"]
    assert all("withheld_reason" in change for change in payload["withheld"])
    assert payload["confidence"]["verdict"] == "auto"


def test_assert_payload_is_safe_catches_a_hand_built_body() -> None:
    """The boundary check, in case something bypasses ``to_payload``."""
    with pytest.raises(SelectionError, match="not exactly the writable field set"):
        assert_payload_is_safe({"Name": "only this"}, "MusicArtist")
