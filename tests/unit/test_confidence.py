"""Match confidence.

Every metadata mistake this tool could make starts here: pulling the wrong artist's
tags onto an item is worse than pulling nothing, because the result looks
authoritative. The tests below are mostly about the *failures* -- what must never be
pre-selected, and what must be refused outright.
"""

from __future__ import annotations

import pytest

from metaedit.domain.confidence import (
    AUTO_THRESHOLD,
    REVIEW_THRESHOLD,
    CandidateIdentity,
    Confidence,
    MatchContext,
    artist_agreement,
    comparable_name,
    duration_agreement,
    mbid_score,
    name_similarity,
    normalize_for_comparison,
    score,
    year_agreement,
)


def artist(name: str, **kwargs: object) -> MatchContext:
    return MatchContext(kind="MusicArtist", name=name, **kwargs)  # type: ignore[arg-type]


def album(name: str, **kwargs: object) -> MatchContext:
    return MatchContext(kind="MusicAlbum", name=name, **kwargs)  # type: ignore[arg-type]


def track(name: str, **kwargs: object) -> MatchContext:
    return MatchContext(kind="Audio", name=name, **kwargs)  # type: ignore[arg-type]


# ------------------------------------------------------------- normalisation


def test_normalisation_folds_case_punctuation_and_accents() -> None:
    assert normalize_for_comparison("Björk") == "bjork"
    assert normalize_for_comparison("AC/DC") == "ac dc"
    assert normalize_for_comparison("  The   BEATLES! ") == "the beatles"


@pytest.mark.parametrize("value", [None, "", "   ", "!!!"])
def test_normalisation_of_nothing_is_empty(value: str | None) -> None:
    assert normalize_for_comparison(value) == ""


# --------------------------------------------------------- name similarity


def test_identical_names_score_one() -> None:
    assert name_similarity("Radiohead", "Radiohead") == 1.0


def test_identical_after_normalisation_scores_one() -> None:
    assert name_similarity("Sigur Rós", "sigur ros") == 1.0
    assert name_similarity("AC/DC", "AC DC") == 1.0


def test_reordered_names_are_recognised() -> None:
    """``"Beatles, The"`` and ``"The Beatles"`` are the same artist.

    A token-set comparison catches this; a character comparison alone would not.
    """
    assert name_similarity("Beatles, The", "The Beatles") == 1.0


def test_extra_words_do_not_destroy_a_match() -> None:
    """A featured artist or a remaster suffix should not break recognition."""
    assert name_similarity("Karma Police", "Karma Police (Remastered)") > 0.6


def test_small_typos_still_match() -> None:
    assert name_similarity("Radiohead", "Radiohed") > 0.8


def test_unrelated_names_score_low() -> None:
    assert name_similarity("Radiohead", "Nickelback") < 0.3


@pytest.mark.parametrize(("left", "right"), [(None, "x"), ("x", None), (None, None), ("", "x")])
def test_missing_names_score_zero(left: str | None, right: str | None) -> None:
    assert name_similarity(left, right) == 0.0


def test_name_similarity_is_symmetric() -> None:
    assert name_similarity("Radiohead", "Radiohed") == name_similarity("Radiohed", "Radiohead")


# ---------------------------------------------------------------- mbid score


def test_matching_mbids_score_one() -> None:
    value, conflict = mbid_score(item_mbid="abc", candidate_mbid="ABC")
    assert value == 1.0
    assert conflict is False


def test_conflicting_mbids_are_reported_as_a_conflict() -> None:
    """Not merely a low score: two ids mean two provably different entities."""
    value, conflict = mbid_score(item_mbid="abc", candidate_mbid="def")
    assert value == 0.0
    assert conflict is True


@pytest.mark.parametrize(
    ("item", "candidate"), [(None, "abc"), ("abc", None), (None, None), ("", "")]
)
def test_missing_ids_are_neutral_not_punishing(item: str | None, candidate: str | None) -> None:
    """A library without MBIDs must not be scored as though every match failed."""
    value, conflict = mbid_score(item_mbid=item, candidate_mbid=candidate)
    assert value == 0.5
    assert conflict is False


# ------------------------------------------------------- component helpers


def test_artist_agreement_uses_the_best_credited_artist() -> None:
    assert artist_agreement(item_artists=["Cher", "Sonny"], candidate_artist="Sonny") == 1.0


def test_artist_agreement_is_none_without_both_sides() -> None:
    assert artist_agreement(item_artists=[], candidate_artist="Cher") is None
    assert artist_agreement(item_artists=["Cher"], candidate_artist=None) is None


@pytest.mark.parametrize(
    ("item", "candidate", "expected"),
    [
        (1997, 1997, 1.0),
        (1997, 1998, 0.8),
        (1997, 1999, 0.4),
        (1997, 2020, 0.0),
    ],
)
def test_year_agreement_tolerates_a_year(item: int, candidate: int, expected: float) -> None:
    """Release dates and reissues routinely disagree by a year between sources."""
    assert year_agreement(item_year=item, candidate_year=candidate) == expected


def test_year_agreement_is_none_when_unknown() -> None:
    assert year_agreement(item_year=None, candidate_year=1997) is None
    assert year_agreement(item_year=1997, candidate_year=None) is None


def test_duration_agreement_within_tolerance() -> None:
    assert duration_agreement(item_duration_ms=261_000, candidate_duration_ms=263_000) == 1.0


def test_duration_agreement_flags_a_different_version() -> None:
    """A remaster or live version is a different recording."""
    assert duration_agreement(item_duration_ms=261_000, candidate_duration_ms=600_000) == 0.0


def test_duration_agreement_is_ambiguous_near_the_boundary() -> None:
    assert duration_agreement(item_duration_ms=261_000, candidate_duration_ms=280_000) == 0.5


def test_duration_agreement_is_none_when_unknown() -> None:
    assert duration_agreement(item_duration_ms=None, candidate_duration_ms=1) is None


# ------------------------------------------------------------------ scoring


def test_an_exact_artist_match_is_auto() -> None:
    result = score(artist("Radiohead", mbid="m1"), CandidateIdentity("Radiohead", mbid="m1"))
    assert result.total == 1.0
    assert result.verdict == "auto"
    assert result.accepts_by_default is True
    assert result.mbid_conflict is False


def test_an_exact_album_match_is_auto() -> None:
    result = score(
        album("OK Computer", mbid="a1", artists=["Radiohead"], year=1997),
        CandidateIdentity("OK Computer", mbid="a1", artist="Radiohead", year=1997),
    )
    assert result.total == 1.0
    assert result.accepts_by_default is True


def test_an_exact_track_match_is_auto() -> None:
    result = score(
        track("Karma Police", mbid="t1", artists=["Radiohead"], year=1997, duration_ms=261_000),
        CandidateIdentity(
            "Karma Police", mbid="t1", artist="Radiohead", year=1997, duration_ms=261_000
        ),
    )
    assert result.total == 1.0
    assert result.accepts_by_default is True


def test_a_wrong_artist_is_rejected() -> None:
    result = score(artist("Radiohead"), CandidateIdentity("Nickelback"))
    assert result.verdict == "reject"
    assert result.accepts_by_default is False, "nothing may be pre-selected"


def test_a_conflicting_mbid_is_rejected_despite_a_perfect_name() -> None:
    """The single most important property here.

    Identical names and contradictory ids must refuse, not average out to a
    confident-looking number. Averages are exactly how the wrong entity gets
    written onto an item.
    """
    result = score(
        artist("Radiohead", mbid="item-id"),
        CandidateIdentity("Radiohead", mbid="different-id"),
    )
    assert result.mbid_conflict is True
    assert result.verdict == "reject"
    assert result.accepts_by_default is False
    assert result.total < AUTO_THRESHOLD
    assert any("disagree" in note for note in result.notes)
    assert any(
        component.name == "mbid" and component.value == 0.0 for component in result.components
    )


def test_a_perfect_name_without_ids_requires_review_not_auto() -> None:
    """A documented consequence, not an accident.

    With no ids on either side the MBID component is neutral, so a perfect name
    match cannot reach ``auto``. Without ids we can infer identity but not confirm
    it, so a human decides. The trade-off is that an MBID-less library always needs
    review -- which is why the neutral value is a named, tested constant rather than
    a magic number.
    """
    result = score(artist("Radiohead"), CandidateIdentity("Radiohead", mbid="candidate-id"))
    assert result.verdict == "review"
    assert result.accepts_by_default is False
    assert result.total < AUTO_THRESHOLD


def test_weights_are_renormalised_for_an_artist() -> None:
    """An artist has no year or duration, so it must not be capped below 1.0."""
    result = score(artist("Radiohead", mbid="m1"), CandidateIdentity("Radiohead", mbid="m1"))
    assert result.total == pytest.approx(1.0)
    applied = sum(component.applied_weight for component in result.components)
    assert applied == pytest.approx(1.0)


def test_weights_are_renormalised_for_a_track() -> None:
    context = track("Karma Police", mbid="t1", artists=["Radiohead"], year=1997)
    result = score(context, CandidateIdentity("Karma Police", mbid="t1", artist="Radiohead"))
    assert result.total == pytest.approx(1.0), "duration is excluded, the rest renormalise"


def test_unavailable_components_are_reported_not_silently_dropped() -> None:
    """An album with no year on either side leaves that component out of the total.

    The component still exists -- it applies to albums -- but has nothing to
    compare, and the note says so rather than the omission being invisible.
    """
    result = score(
        album("OK Computer", mbid="a1", artists=["Radiohead"]),
        CandidateIdentity("OK Computer", mbid="a1", artist="Radiohead"),
    )
    assert any("not compared" in note and "year" in note for note in result.notes)
    assert any(
        component.name == "year" and component.value is None for component in result.components
    )


def test_no_comparable_signals_is_rejected() -> None:
    """Nothing to compare must refuse, never guess from an empty comparison."""
    result = score(artist(""), CandidateIdentity(""))
    assert result.verdict == "reject"
    assert result.total == 0.0
    assert result.accepts_by_default is False
    assert any("no comparable signals" in note for note in result.notes)


def test_a_missing_name_is_not_scored_as_a_mismatch() -> None:
    """Absent data is not disagreement.

    Scoring an absent name as 0.0 penalised a candidate for a field the response did
    not carry, while an absent year was correctly treated as neutral. This is the
    inconsistency that hid it: one case means "we compared and they differ", the
    other means "we could not compare".
    """
    assert comparable_name("", "Radiohead") is None
    assert comparable_name("Radiohead", "") is None
    assert comparable_name(None, None) is None
    assert comparable_name("Radiohead", "Radiohead") == 1.0


def test_a_candidate_with_one_usable_signal_still_scores_on_it() -> None:
    """The remaining signal is renormalised, not diluted by the absent one."""
    result = score(artist("", mbid="m1"), CandidateIdentity("", mbid="m1"))
    assert result.total == 1.0, "only the mbid is comparable, and it agrees"
    assert result.verdict == "auto"


def test_verdict_thresholds_are_the_documented_ones() -> None:
    assert AUTO_THRESHOLD == 0.85
    assert REVIEW_THRESHOLD == 0.60


@pytest.mark.parametrize("kind", ["MusicArtist", "MusicAlbum", "Audio"])
def test_the_explanation_names_the_contributing_signals(kind: str) -> None:
    context = MatchContext(
        kind=kind, name="X", mbid="m", artists=["Y"], year=2000, duration_ms=1000
    )  # type: ignore[arg-type]
    result: Confidence = score(
        context, CandidateIdentity("X", mbid="m", artist="Y", year=2000, duration_ms=1000)
    )
    assert "name=" in result.explanation
    assert "mbid=" in result.explanation


def test_every_component_carries_a_human_readable_detail() -> None:
    result = score(
        album("OK Computer", mbid="a1", artists=["Radiohead"], year=1997),
        CandidateIdentity("OK Computer", mbid="a1", artist="Radiohead", year=1997),
    )
    assert all(component.detail for component in result.components)


def test_contributions_sum_to_the_total() -> None:
    result = score(
        track("Karma Police", mbid="t1", artists=["Radiohead"], year=1997, duration_ms=261_000),
        CandidateIdentity(
            "Karma Police", mbid="t1", artist="Radiohead", year=1997, duration_ms=261_000
        ),
    )
    total = sum(component.contribution for component in result.components)
    assert result.total == pytest.approx(total, abs=0.001)


def test_a_remastered_track_lands_in_review_not_auto() -> None:
    """Right song, different recording: a human should decide, not the arithmetic."""
    result = score(
        track("Karma Police", artists=["Radiohead"], duration_ms=261_000),
        CandidateIdentity("Karma Police", artist="Radiohead", duration_ms=600_000),
    )
    assert result.verdict in {"review", "reject"}
    assert result.accepts_by_default is False


def test_scoring_is_deterministic() -> None:
    context = album("OK Computer", mbid="a1", artists=["Radiohead"], year=1997)
    candidate = CandidateIdentity("OK Computer", mbid="a1", artist="Radiohead", year=1997)
    assert score(context, candidate) == score(context, candidate)
