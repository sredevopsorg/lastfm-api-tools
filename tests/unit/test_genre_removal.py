"""Removing a genre value from a field.

Weighted towards what must *not* be removed. Both directions of a mistake here are
silent: too narrow and the batch reports "changed 0 items" for a genre the operator can
see in Jellyfin; too wide and it deletes genres nobody named, across a whole library, with
a snapshot to undo it that nobody will think to look for.

Every genre string in these tests is taken from the live library rather than invented,
because the awkward ones -- packed commas, semicolons, slashes inside a component -- are
exactly the cases an invented fixture would omit.
"""

from __future__ import annotations

import pytest

from metaedit.domain.genre_removal import (
    RemovalOutcome,
    matches,
    normalise,
    plan_removal,
    plan_removals,
    remove_component,
    remove_value,
    split_packed_values,
)

# Live values. `PACKED_COMMA` is one album's genre; `PLAIN_FRAGMENT` and `OTHER_FRAGMENT`
# are separately-occurring genres on the live library (67 and 10 albums respectively).
PACKED_COMMA = "Rock, Reggae"
PLAIN_FRAGMENT = "Rock"
OTHER_FRAGMENT = "Reggae"
PACKED_LONG = "Thrash Metal, Speed Metal, Heavy Metal, Hard Rock"
PACKED_SEMICOLON = "Reggae; Ska"
SLASHED = "Electronic; Rock; Funk / Soul"


# ------------------------------------------------------------------ matching


@pytest.mark.parametrize("spelling", ["rock", "ROCK", "RoCk", "  rock  "])
def test_matching_folds_case_and_whitespace(spelling: str) -> None:
    assert matches("Rock", spelling)
    assert matches(spelling, "Rock")


def test_matching_is_exact_and_never_a_substring() -> None:
    """The distinction that protects the library, measured.

    ``Genres=Rock`` returns 67 albums on the live server; ``Genres=Rock, Reggae``
    returns 1. A substring matcher would delete 67 albums' worth of curation for a
    request naming a genre that occurs once.
    """
    assert matches("Rock", "Rock")
    assert not matches(PACKED_COMMA, PLAIN_FRAGMENT)
    assert not matches("Gothic Rock", "Rock")
    assert not matches("Rockabilly", "Rock")
    assert not matches(PLAIN_FRAGMENT, PACKED_COMMA)


def test_a_packed_value_is_not_matched_by_its_components() -> None:
    assert not matches(PACKED_LONG, "Heavy Metal")
    assert not matches(PACKED_LONG, "Hard Rock")
    assert matches(PACKED_LONG, PACKED_LONG)


def test_a_blank_target_matches_nothing() -> None:
    """An empty target must never behave as a wildcard.

    A request that removed *every* genre because it left the value blank is the one
    failure here that is library-wide, so it is refused at the boundary and inert here.
    """
    for blank in ("", "   ", None):
        assert not matches("Rock", blank)
        assert not matches("", blank)


def test_matching_uses_the_archive_normalisation() -> None:
    from metaedit.adapters.lastfm.canonical import normalize_tag

    for value in ["Rock", "  Gothic   Rock  ", "R&B/Soul", "Café Tacvba", "İstanbul"]:
        assert normalise(value) == normalize_tag(value)


# --------------------------------------------------------------- remove_value


def test_remove_value_removes_only_the_named_value() -> None:
    kept, removed = remove_value(["Rock", "Ska", "Gothic Rock"], target="rock")
    assert removed == ["Rock"]
    assert kept == ["Ska", "Gothic Rock"]


def test_remove_value_preserves_order() -> None:
    """Jellyfin's genre order is what clients display, so a removal must not reshuffle."""
    kept, _ = remove_value(["Zebra", "Apple", "Mango"], target="nothing")
    assert kept == ["Zebra", "Apple", "Mango"]


def test_remove_value_removes_every_copy() -> None:
    """A duplicate left behind looks exactly like the removal having failed."""
    kept, removed = remove_value(["Rock", "Ska", "rock", "ROCK"], target="Rock")
    assert removed == ["Rock", "rock", "ROCK"]
    assert kept == ["Ska"]


def test_remove_value_leaves_a_packed_value_intact() -> None:
    kept, removed = remove_value([PACKED_COMMA, "Ska"], target=PLAIN_FRAGMENT)
    assert removed == []
    assert kept == [PACKED_COMMA, "Ska"]


def test_remove_value_removes_a_packed_value_when_named_whole() -> None:
    kept, removed = remove_value([PACKED_COMMA, "Ska"], target=PACKED_COMMA)
    assert removed == [PACKED_COMMA]
    assert kept == ["Ska"]


def test_remove_value_handles_none_and_empty() -> None:
    assert remove_value(None, target="Rock") == ([], [])
    assert remove_value([], target="Rock") == ([], [])


def test_remove_value_does_not_guard_against_non_strings() -> None:
    """Documents a deliberate absence, so nobody "fixes" it back in.

    `domain.snapshot` normalises Jellyfin's arrays to strings, so a non-string here would
    be a normalisation regression. A defensive isinstance would silently keep it and hide
    the regression; mypy already reports the guard as unreachable. The declared type is
    the guarantee, and there is no runtime check to test -- only this note that its
    absence is intentional.
    """
    assert remove_value([], target="Rock") == ([], [])


# --------------------------------------------------------------- plan_removal


def test_plan_removal_reports_what_it_would_do() -> None:
    outcome = plan_removal(["Rock", "Ska"], target="Rock", field="Genres")
    assert outcome.field == "Genres"
    assert outcome.target == "Rock"
    assert outcome.before == ["Rock", "Ska"]
    assert outcome.after == ["Ska"]
    assert outcome.removed == ["Rock"]
    assert outcome.changed is True
    assert outcome.emptied is False


def test_plan_removal_of_an_absent_value_changes_nothing() -> None:
    outcome = plan_removal(["Ska"], target="Rock", field="Genres")
    assert outcome.changed is False
    assert outcome.removed == []
    assert outcome.after == ["Ska"]


def test_plan_removal_flags_an_emptied_field() -> None:
    """Clearing a field is legitimate and revertible, but it is the outcome most likely
    to be unintended, so it is reported rather than discovered."""
    outcome = plan_removal(["Rock"], target="Rock", field="Genres")
    assert outcome.emptied is True
    assert outcome.after == []


def test_an_empty_field_is_not_reported_as_emptied() -> None:
    """Nothing was removed, so nothing was emptied -- the distinction matters to a UI
    deciding whether to warn."""
    outcome = plan_removal([], target="Rock", field="Genres")
    assert outcome.changed is False
    assert outcome.emptied is False


def test_plan_removal_is_json_serialisable() -> None:
    import json

    payload = plan_removal(["Rock"], target="Rock", field="Genres").as_dict()
    assert json.loads(json.dumps(payload))["removed"] == ["Rock"]


# ------------------------------------------------------------- plan_removals


def test_plan_removals_covers_every_requested_field() -> None:
    item = {"Genres": ["Rock", "Ska"], "Tags": ["britpop"]}
    outcomes = plan_removals(item, target="Rock", fields=("Genres", "Tags"))

    assert [o.field for o in outcomes] == ["Genres", "Tags"]
    assert outcomes[0].changed is True
    assert outcomes[1].changed is False


def test_plan_removals_reports_an_absent_field_rather_than_omitting_it() -> None:
    """An omitted row is indistinguishable from a bug; "not present" is an answer."""
    outcomes = plan_removals({}, target="Rock", fields=("Genres", "Tags"))
    assert [o.field for o in outcomes] == ["Genres", "Tags"]
    assert all(not o.changed for o in outcomes)
    assert all(o.before == [] for o in outcomes)


def test_plan_removals_ignores_a_non_list_field() -> None:
    outcomes = plan_removals({"Genres": "Rock"}, target="Rock", fields=("Genres",))
    assert outcomes[0].changed is False


# ------------------------------------------------------- packed value splitting


def test_split_packed_values_uses_the_policy_pattern() -> None:
    assert split_packed_values(PACKED_COMMA) == ["Rock", "Reggae"]
    assert split_packed_values(PACKED_LONG) == [
        "Thrash Metal",
        "Speed Metal",
        "Heavy Metal",
        "Hard Rock",
    ]
    assert split_packed_values(PACKED_SEMICOLON) == ["Reggae", "Ska"]
    assert split_packed_values(SLASHED) == ["Electronic", "Rock", "Funk", "Soul"]


def test_split_of_a_single_value_is_itself() -> None:
    assert split_packed_values("Gothic Rock") == ["Gothic Rock"]


def test_remove_component_drops_one_from_a_packed_value() -> None:
    outcome = remove_component(PACKED_COMMA, target="Reggae")
    assert outcome.removed_components == ["Reggae"]
    assert outcome.kept_components == ["Rock"]
    assert outcome.replacement == "Rock", "rebuilt as a string, not a list of one"
    assert outcome.changed is True


def test_remove_component_reuses_the_original_separator() -> None:
    """Rewriting ``"Reggae; Ska"`` as ``"Reggae, Ska"`` would be an unrelated restyle of
    a value the operator did not ask to change."""
    outcome = remove_component(PACKED_SEMICOLON, target="Ska")
    assert outcome.replacement == "Reggae"

    three = remove_component("Reggae; Ska; Rock", target="Rock")
    assert three.replacement == "Reggae; Ska"


def test_remove_component_keeps_a_comma_separated_style() -> None:
    outcome = remove_component(PACKED_LONG, target="Hard Rock")
    assert outcome.replacement == "Thrash Metal, Speed Metal, Heavy Metal"


def test_remove_component_returns_none_when_nothing_survives() -> None:
    """The caller decides whether that means "drop the value" or "leave it" -- this
    function does not guess."""
    outcome = remove_component("Ska", target="Ska")
    assert outcome.removed_components == ["Ska"]
    assert outcome.replacement is None


def test_remove_component_with_no_match_is_a_no_op() -> None:
    outcome = remove_component(PACKED_COMMA, target="Jazz")
    assert outcome.changed is False
    assert outcome.replacement == PACKED_COMMA
    assert outcome.components == ["Rock", "Reggae"]


def test_remove_component_drops_every_matching_component() -> None:
    outcome = remove_component("Rock, Ska, Rock", target="rock")
    assert outcome.removed_components == ["Rock", "Rock"]
    assert outcome.replacement == "Ska"


def test_remove_component_is_exact_within_a_component() -> None:
    """Splitting must not become substring matching inside a component."""
    outcome = remove_component("Gothic Rock, Rockabilly", target="Rock")
    assert outcome.changed is False
    assert outcome.replacement == "Gothic Rock, Rockabilly"


def test_remove_component_handles_a_slashed_component() -> None:
    outcome = remove_component(SLASHED, target="Funk")
    assert outcome.replacement == "Electronic; Rock; Soul"


def test_a_slashed_genre_name_survives_packed_splitting() -> None:
    """``R&B/Soul`` is one genre whose name contains a slash, and ``SPLIT_PATTERN`` only
    splits on `` / `` with spaces -- so a name like this is not torn in half."""
    assert split_packed_values("R&B/Soul") == ["R&B/Soul"]


def test_split_outcome_is_frozen() -> None:
    from dataclasses import FrozenInstanceError

    outcome = remove_component("Rock, Reggae", target="Rock")
    with pytest.raises(FrozenInstanceError):
        outcome.original = "other"  # type: ignore[misc]


def test_removal_outcome_is_frozen() -> None:
    from dataclasses import FrozenInstanceError

    outcome = RemovalOutcome(
        field="Genres", target="Rock", before=["Rock"], after=[], removed=["Rock"]
    )
    with pytest.raises(FrozenInstanceError):
        outcome.field = "Tags"  # type: ignore[misc]
