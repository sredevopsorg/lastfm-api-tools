"""The genre blacklist: parsing and matching.

The tests are weighted towards what must *not* match, because both failure modes of
this module are silent data loss:

* a substring matcher would drop ``"Rock, Reggae"`` when ``"Rock"`` is blacklisted;
* a comma-splitting parser would turn that one genre into two entries, with the same
  effect one step earlier.

Neither raises. Both just delete genres the operator never named, which is why every
one of those cases is pinned here against a value measured from a live library.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from metaedit.domain.genre_blacklist import (
    BlacklistConflictError,
    Conflict,
    Entry,
    conflicts,
    matches,
    matches_any_piece,
    normalise,
    parse,
    parse_strict,
)

# Genre values taken from a live Jellyfin 12.2.0 music library. These are the real
# shapes, commas and slashes included -- not invented fixtures.
REAL_COMMA_GENRE = "Rock, Reggae"
REAL_COMMA_GENRE_LONG = "Hip Hop, Rock, Latin, Funk / Soul, Pop, Children's, Folk, World, & Country"
REAL_SEMICOLON_GENRE = "Alternative Rock; Electroclash; Experimental Music; Gothic Rock"
REAL_PLAIN = "Gothic Rock"


# ------------------------------------------------------------------ normalising


@pytest.mark.parametrize(
    "raw",
    ["Rock", "rock", "ROCK", "  rock  ", "RoCk"],
)
def test_case_and_whitespace_fold_to_one_key(raw: str) -> None:
    assert normalise(raw) == "rock"


def test_normalisation_matches_the_archive_tag_key() -> None:
    """Same function as ``lastfm_tag_edge.tag_name_norm``, by construction.

    A second normaliser is how the blacklist and the archived tag graph would come to
    disagree about whether two strings are the same tag.
    """
    from metaedit.adapters.lastfm.canonical import normalize_tag

    for value in ["Rock", "  Alternative   Rock ", "R&B/Soul", "Café Tacvba", "İstanbul"]:
        assert normalise(value) == normalize_tag(value)


def test_normalisation_is_idempotent() -> None:
    once = normalise("  RoCk   &  Roll ")
    assert normalise(once) == once


# -------------------------------------------------------------------- matching


def test_matches_ignores_case_in_both_directions() -> None:
    assert matches("rock", {"Rock"})
    assert matches("Rock", {"rock"})
    assert matches("ROCK", {"rOcK"})


def test_matches_exactly_and_never_as_a_substring() -> None:
    """``"rock"`` must not match ``"gothic rock"`` or ``"rockabilly"``."""
    entries = {"rock"}
    assert matches("rock", entries)
    assert not matches("gothic rock", entries)
    assert not matches("rockabilly", entries)
    assert not matches("progressive rock", entries)


def test_a_comma_genre_is_a_different_value_from_its_first_fragment() -> None:
    """The load-bearing case, measured: ``Genres=Rock`` returns 67 albums on the live
    library while ``Genres=Rock, Reggae`` returns 1. Treating the first as matching the
    second would delete 67 albums' worth of curation for one request."""
    entries = {"rock"}
    assert not matches(REAL_COMMA_GENRE, entries)
    assert not matches(REAL_COMMA_GENRE_LONG, entries)

    # And the reverse: blacklisting the full value must not match the plain fragment.
    full = {normalise(REAL_COMMA_GENRE)}
    assert matches(REAL_COMMA_GENRE, full)
    assert not matches("Reggae", full)
    assert not matches("Rock", full)


def test_matches_a_long_comma_genre_verbatim() -> None:
    entries = {normalise(REAL_COMMA_GENRE_LONG)}
    assert matches(REAL_COMMA_GENRE_LONG, entries)
    assert matches(REAL_COMMA_GENRE_LONG.casefold(), entries)


def test_blank_candidates_never_match() -> None:
    assert not matches("", {"", "rock"})
    assert not matches("   ", {"rock"})
    assert not matches(None, {"rock"})


def test_slash_and_semicolon_genres_are_matched_whole() -> None:
    """``SPLIT_PATTERN`` splits these for *classifying Last.fm tags*. Blacklist matching
    must not: the operator blacklisted the string Jellyfin stores."""
    entries = {normalise(REAL_SEMICOLON_GENRE)}
    assert matches(REAL_SEMICOLON_GENRE, entries)
    assert not matches("Gothic Rock", entries)
    assert not matches("Alternative Rock", entries)


def test_matching_against_raw_entries_still_folds_case() -> None:
    """``entries`` may arrive unnormalised; both sides fold, so it costs nothing."""
    assert matches("Gothic Rock", {"gothic rock", "  Chill  "})


# ------------------------------------------------------- piece-level matching
#
# `SPLIT_PATTERN` decomposes a multi-valued tag on ``,``, ``;``, ``|`` and `` / `` before
# the policy ever sees it, so `"Rock, Reggae"` becomes two genres. `matches_any_piece` is
# the write-path question; `matches` is the "is this literal string blacklisted" question.
# Conflating them is how a blacklist silently blacklists nothing.


def test_matches_any_piece_finds_the_piece_the_operator_named() -> None:
    assert matches_any_piece(REAL_COMMA_GENRE, {"rock"})
    assert matches_any_piece(REAL_COMMA_GENRE, {"reggae"})
    assert not matches_any_piece(REAL_COMMA_GENRE, {"gothic"})


def test_matches_whole_string_and_matches_any_piece_disagree_by_design() -> None:
    """The distinction, stated once so it cannot be "fixed" into agreement."""
    entries = {"rock"}
    assert matches_any_piece(REAL_COMMA_GENRE, entries) is True
    assert matches(REAL_COMMA_GENRE, entries) is False


def test_matches_any_piece_splits_on_every_separator_the_policy_uses() -> None:
    entries = {"rock", "funk", "soul"}
    for value in [
        "Rock, Reggae",
        "Rock; Latin Rock",
        "Electronic; Rock; Funk / Soul",
        "A|Rock",
    ]:
        assert matches_any_piece(value, entries), value


def test_matches_any_piece_is_exact_per_piece() -> None:
    """Splitting must not become a licence for substring matching within a piece."""
    assert not matches_any_piece("Gothic Rock, Ska", {"rock"})
    assert not matches_any_piece("Rockabilly, Ska", {"rock"})
    assert matches_any_piece("Gothic Rock, Rock", {"rock"})


def test_matches_any_piece_handles_single_and_empty_values() -> None:
    assert matches_any_piece("Rock", {"rock"})
    assert not matches_any_piece("", {"rock"})
    assert not matches_any_piece(None, {"rock"})
    assert not matches_any_piece("  , ;  ", {"rock"})


# --------------------------------------------------------------------- parsing


def test_one_entry_per_line_is_the_unambiguous_format() -> None:
    result = parse("Rock\nReggae\nGothic Rock")
    assert result.values == ["Rock", "Reggae", "Gothic Rock"]
    assert result.clean
    assert len(result.entries) == 3


def test_a_comma_line_is_reported_as_a_conflict_not_split() -> None:
    """The operator's example format, and the reason it cannot be split blindly."""
    result = parse(REAL_COMMA_GENRE)
    assert result.entries == []
    assert not result.clean
    assert len(result.conflicts) == 1

    conflict = result.conflicts[0]
    assert conflict.raw == REAL_COMMA_GENRE
    assert conflict.whole == REAL_COMMA_GENRE
    assert conflict.fragments == ["Rock", "Reggae"]
    assert "comma" in conflict.message


def test_the_operators_own_example_reports_the_ambiguity() -> None:
    """``rock, a blacklisted genre, etc`` -- the phrasing from the request.

    Two of its three fragments are nonsense genres and one is a real one. Splitting
    would blacklist ``etc``, which is harmless; the danger is the general case, so the
    input is refused rather than guessed at regardless of whether this instance is.
    """
    result = parse("rock, a blacklisted genre, etc")
    assert not result.clean
    assert result.conflicts[0].fragments == ["rock", "a blacklisted genre", "etc"]


def test_allow_commas_splits_only_when_the_caller_has_decided() -> None:
    result = parse("rock, reggae", allow_commas=True)
    assert result.values == ["rock", "reggae"]
    assert result.clean


def test_conflicting_and_clean_lines_coexist() -> None:
    result = parse("Gothic\n" + REAL_COMMA_GENRE + "\nChill")
    assert result.values == ["Gothic", "Chill"]
    assert len(result.conflicts) == 1


def test_blank_lines_are_layout_not_entries() -> None:
    result = parse("\n\nRock\n\n\n")
    assert result.values == ["Rock"]
    assert result.blanks == 4
    assert result.clean


def test_whitespace_only_input_yields_nothing() -> None:
    result = parse("   \n\t\n  ")
    assert result.entries == []
    assert result.clean


def test_duplicates_are_removed_case_insensitively_keeping_first_spelling() -> None:
    """The table is unique on the normalised key, so this must be settled in parsing
    rather than surfacing as an IntegrityError after a user has saved."""
    result = parse("Rock\nrock\nROCK\n  rock  ")
    assert result.values == ["Rock"]
    assert len(result.entries) == 1


def test_a_different_entry_is_not_deduplicated_away() -> None:
    result = parse("Rock\nReggae")
    assert result.values == ["Rock", "Reggae"]


def test_lines_are_stripped_but_inner_whitespace_is_preserved_in_the_key() -> None:
    result = parse("  Gothic   Rock  ")
    assert result.values == ["Gothic   Rock"]
    assert result.normalised == {"gothic rock"}


def test_normalised_exposes_the_comparison_keys() -> None:
    result = parse("Rock\nGothic Rock")
    assert result.normalised == frozenset({"rock", "gothic rock"})


# -------------------------------------------------------------------- conflicts


def test_conflicts_only_reports_lines_containing_the_separator() -> None:
    found = conflicts(["Rock", "Rock, Reggae", "  ", "Gothic"])
    assert len(found) == 1
    assert found[0].whole == "Rock, Reggae"


def test_conflicts_prefers_a_live_genre_spelling_for_whole() -> None:
    """``whole`` should name the value the operator almost certainly meant, so the UI can
    show the exact library genre rather than their typo'd line."""
    found = conflicts(["rock, reggae"], known={REAL_COMMA_GENRE})
    assert found[0].whole == REAL_COMMA_GENRE
    assert found[0].raw == "rock, reggae"
    assert found[0].fragments == ["rock", "reggae"]


def test_conflicts_falls_back_to_the_raw_line_when_nothing_matches() -> None:
    found = conflicts(["Zzz, Yyy"], known={REAL_COMMA_GENRE})
    assert found[0].whole == "Zzz, Yyy"


def test_a_slash_genre_is_not_a_conflict() -> None:
    """Only the configured separator is ambiguous. ``R&B/Soul`` is one genre."""
    assert conflicts(["Electronic; Rock; Funk / Soul"]) == []


# ----------------------------------------------------------------- strict parse


def test_strict_parse_raises_with_every_conflict_and_a_422() -> None:
    with pytest.raises(BlacklistConflictError) as caught:
        parse_strict(REAL_COMMA_GENRE + "\nPopup, Bonus")

    error = caught.value
    assert error.http_status == 422
    assert error.code == "ambiguous_blacklist_entry"
    assert len(error.conflicts) == 2
    assert "Refusing to guess" in error.message
    # The message must name the values, or the client cannot offer a choice.
    assert REAL_COMMA_GENRE in error.message


def test_strict_parse_succeeds_on_unambiguous_input() -> None:
    result = parse_strict("Rock\nReggae")
    assert result.values == ["Rock", "Reggae"]


def test_strict_parse_message_is_singular_for_one_conflict() -> None:
    with pytest.raises(BlacklistConflictError) as caught:
        parse_strict(REAL_COMMA_GENRE)
    assert "1 entry" in caught.value.message
    assert "entries" not in caught.value.message


# -------------------------------------------------------------------- summary


def test_summary_is_json_serialisable_for_the_api_response() -> None:
    import json

    result = parse("Gothic\n" + REAL_COMMA_GENRE)
    payload = result.summary()
    assert json.loads(json.dumps(payload))["count"] == 1


def test_entry_reports_whether_it_contains_the_separator() -> None:
    assert Entry(value=REAL_COMMA_GENRE, norm=normalise(REAL_COMMA_GENRE)).has_separator
    assert not Entry(value="Rock", norm="rock").has_separator


def test_conflict_dataclass_is_frozen() -> None:
    conflict = Conflict(raw="a,b", whole="a,b", fragments=["a", "b"])
    with pytest.raises(FrozenInstanceError):
        conflict.raw = "other"  # type: ignore[misc]
