"""Exclusion patterns: what they match, and what they deliberately do not.

The load-bearing assertions are the negative ones. ``live`` matching ``Live at Leeds``
would look reasonable in a demo and would silently drop items on a real library, which is
the failure mode ADR 0015 exists to prevent one field over.
"""

from __future__ import annotations

import pytest

from metaedit.adapters.jellyfin.dto import BaseItemDto
from metaedit.domain.errors import ValidationError
from metaedit.domain.exclusion import (
    MAX_PATTERN_LENGTH,
    MAX_PATTERNS,
    compile_patterns,
    exclusion_reason,
    is_excluded,
    matched_pattern,
)
from metaedit.service.labels import labels_from_dto, labels_from_summary


class Summary:
    """Stands in for the API's pydantic ``ItemSummary``: only the three fields matter."""

    def __init__(
        self, name: str, album: str | None = None, album_artist: str | None = None
    ) -> None:
        self.name = name
        self.album = album
        self.album_artist = album_artist


# ------------------------------------------------------------------ the two adapters


def test_both_adapters_read_the_same_fields() -> None:
    # The anti-drift guarantee. If the summary and the DTO readers disagree, an exclusion
    # works on the browse screen and not in a batch -- invisible until a write lands on
    # something the operator had filtered out.
    summary = Summary("Choose Life", "Trainspotting", "Various Artists")
    dto = BaseItemDto.model_validate(
        {
            "Id": "a" * 32,
            "Name": "Choose Life",
            "Album": "Trainspotting",
            "AlbumArtist": "Various Artists",
        }
    )

    assert set(labels_from_summary(summary)) == set(labels_from_dto(dto))


def test_a_dtos_album_artist_is_read_from_whichever_spelling_the_server_sent() -> None:
    # Jellyfin sends `AlbumArtists`, `AlbumArtist` or `Artists` depending on the endpoint
    # and version; the DTO's own accessor is what decides, so this reads the same field the
    # write path does.
    for payload in (
        {"AlbumArtists": [{"Id": "b" * 32, "Name": "Various Artists"}]},
        {"AlbumArtist": "Various Artists"},
        {"Artists": ["Various Artists"]},
    ):
        dto = BaseItemDto.model_validate({"Id": "a" * 32, "Name": "Track", **payload})
        assert "Various Artists" in labels_from_dto(dto)


def test_labels_are_deduplicated_and_blanks_dropped() -> None:
    dto = BaseItemDto.model_validate(
        {"Id": "a" * 32, "Name": "Same", "Album": "  ", "AlbumArtist": "same"}
    )
    assert labels_from_dto(dto) == ("Same",)


# ------------------------------------------------------------------ matching


def test_a_bare_word_matches_only_the_whole_label() -> None:
    patterns = compile_patterns(["live"])
    assert is_excluded(["Live"], patterns)
    assert not is_excluded(["Live at Leeds"], patterns)
    assert not is_excluded(["Alive"], patterns)


def test_a_wildcard_is_what_matches_a_substring() -> None:
    patterns = compile_patterns(["*live*"])
    assert is_excluded(["Live at Leeds"], patterns)
    assert is_excluded(["Alive"], patterns)
    assert not is_excluded(["Studio"], patterns)


def test_matching_folds_case_and_unicode() -> None:
    assert is_excluded(["LIVE AT LEEDS"], compile_patterns(["*live*"]))
    assert is_excluded(["Motörhead Live"], compile_patterns(["*MOTÖRHEAD*"]))
    # `nfkd`/`casefold` folding: a German sharp s folds to `ss` on both sides.
    assert is_excluded(["Straße"], compile_patterns(["STRASSE"]))


def test_a_question_mark_matches_exactly_one_character() -> None:
    patterns = compile_patterns(["ab?"])
    assert is_excluded(["abc"], patterns)
    assert not is_excluded(["abcd"], patterns)


def test_any_pattern_against_any_label_excludes() -> None:
    # `Various Artists` appears only in the album artist. Requiring every label to match
    # would make the single most useful exclusion in a music library match nothing.
    labels = ["Choose Life", "Trainspotting", "Various Artists"]
    assert is_excluded(labels, compile_patterns(["Various Artists"]))
    assert is_excluded(labels, compile_patterns(["Trainspotting"]))
    assert is_excluded(labels, compile_patterns(["choose life"]))
    assert not is_excluded(labels, compile_patterns(["Portishead"]))


def test_regex_metacharacters_are_literal_and_not_a_backtracking_surface() -> None:
    # fnmatch escapes everything but the wildcards. If a dot were a regex dot, `.` would
    # match any character and this pattern would exclude everything.
    patterns = compile_patterns([".*"])
    assert is_excluded([".hidden"], patterns)
    assert not is_excluded(["anything"], patterns)

    # A pathological regex would take exponential time; a glob of the same text cannot.
    patterns = compile_patterns(["*a*a*a*a*a*a*a*a*a*a*b"])
    assert not is_excluded(["a" * 60], patterns)


def test_the_matched_pattern_is_reported_not_merely_the_verdict() -> None:
    # "excluded" with no reason is indistinguishable from a bug, and the pattern is often
    # what the operator mistyped.
    patterns = compile_patterns(["*Live*", "Various Artists"])
    assert matched_pattern("Live at Leeds", patterns) == "*live*"
    assert matched_pattern("Portishead", patterns) is None
    assert exclusion_reason(["Portishead"], patterns) is None
    reason = exclusion_reason(["Live at Leeds"], patterns)
    assert reason is not None
    assert "*live*" in reason
    assert "Live at Leeds" in reason


def test_no_patterns_excludes_nothing() -> None:
    # The default. A missing pattern list must not mean "match everything".
    assert not is_excluded(["Anything"], ())
    assert matched_pattern("Anything", ()) is None
    assert matched_pattern("", compile_patterns(["*"])) is None


# ------------------------------------------------------------------ compiling


def test_patterns_are_normalised_so_two_spellings_are_one_rule() -> None:
    assert compile_patterns(["  *LIVE* ", "*live*"]) == ("*live*",)


def test_blank_patterns_are_dropped_rather_than_refused() -> None:
    # A blank line in a textarea is not an instruction.
    assert compile_patterns(["", "  ", "*live*", "\t"]) == ("*live*",)
    assert compile_patterns(None) == ()


def test_an_overlong_pattern_is_refused() -> None:
    with pytest.raises(ValidationError) as caught:
        compile_patterns(["*" * (MAX_PATTERN_LENGTH + 1)])
    assert str(MAX_PATTERN_LENGTH) in caught.value.message
    assert caught.value.http_status == 422


def test_too_many_patterns_are_refused() -> None:
    with pytest.raises(ValidationError) as caught:
        compile_patterns([f"pattern{index}" for index in range(MAX_PATTERNS + 1)])
    assert str(MAX_PATTERNS) in caught.value.message


def test_a_single_wildcard_is_legal_even_when_it_excludes_everything() -> None:
    # Legal on purpose: an operator may want to see the empty state to confirm the control
    # works. The UI is what has to explain a zero, not the domain what has to refuse it.
    patterns = compile_patterns(["*"])
    assert is_excluded(["Anything at all"], patterns)


def test_a_bare_string_is_one_pattern_not_its_characters() -> None:
    """``str`` is an ``Iterable[str]``, so the obvious loop splits it into characters.

    That is not hypothetical: it is how an exclusion-only scan that matched *nothing* was
    found, because ``"*filler*"`` became eight single-character patterns, one of which was
    ``*`` -- and a single ``*`` matches every label there is.
    """
    assert compile_patterns("*filler*") == ("*filler*",)
    # And the consequences, asserted rather than reasoned about: a character list would
    # have excluded this, because '*' is in it.
    assert not is_excluded(["Radiohead"], compile_patterns("*filler*"))
    assert is_excluded(["Filler Artist 001"], compile_patterns("*filler*"))
