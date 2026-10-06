"""Tag policy: the flat Last.fm tag set mapped onto Jellyfin genres and tags.

This is where "genre" and "style" get decided, over a community tag set that is
genuinely noisy. The tests are mostly about what gets *dropped* and *why*, because
a policy that silently discards curated data is worse than one that does nothing.
"""

from __future__ import annotations

import pytest

from metaedit.domain.tags import (
    DEFAULT_BLACKLIST,
    TagInput,
    TagPolicy,
    tags_from_payload,
)


def names(tags: list[TagInput]) -> list[str]:
    return [tag.name for tag in tags]


# ----------------------------------------------------------------- ranking


def test_tags_with_counts_rank_by_popularity() -> None:
    policy = TagPolicy()
    ranked = policy.rank([TagInput("low", 1), TagInput("high", 100), TagInput("mid", 50)])
    assert names(ranked) == ["high", "mid", "low"]


def test_uncounted_tags_keep_their_source_order() -> None:
    """Sorting an uncounted list by count would reorder it arbitrarily.

    ``artist.getInfo`` does not return counts, so list order *is* the ranking; a
    naive sort would scramble it.
    """
    policy = TagPolicy()
    ranked = policy.rank([TagInput("first"), TagInput("second"), TagInput("third")])
    assert names(ranked) == ["first", "second", "third"]


def test_counted_and_uncounted_tags_do_not_interleave() -> None:
    policy = TagPolicy()
    ranked = policy.rank(
        [TagInput("nocount", None), TagInput("counted", 5), TagInput("also-nocount", None)]
    )
    assert names(ranked) == ["counted", "nocount", "also-nocount"]


def test_equal_counts_keep_source_order() -> None:
    policy = TagPolicy()
    ranked = policy.rank([TagInput("a", 10), TagInput("b", 10), TagInput("c", 10)])
    assert names(ranked) == ["a", "b", "c"]


# ----------------------------------------------------------- genre/style split


def test_the_first_tags_become_genres_and_the_rest_styles() -> None:
    policy = TagPolicy(genre_limit=2, style_limit=3)
    outcome = policy.apply(
        [TagInput(name, 100 - index) for index, name in enumerate(["a", "b", "c", "d", "e"])]
    )
    assert outcome.genres == ["a", "b"]
    assert outcome.tags == ["c", "d", "e"]


def test_tags_beyond_both_limits_are_reported_not_silently_lost() -> None:
    policy = TagPolicy(genre_limit=1, style_limit=1)
    outcome = policy.apply(
        [TagInput(name, 10 - index) for index, name in enumerate(["a", "b", "c"])]
    )
    assert outcome.genres == ["a"]
    assert outcome.tags == ["b"]
    assert outcome.overflow == ["c"], "the reason a tag was unused must be visible"


def test_zero_limits_produce_nothing() -> None:
    policy = TagPolicy(genre_limit=0, style_limit=0)
    outcome = policy.apply([TagInput("pop", 10)])
    assert outcome.genres == []
    assert outcome.tags == []


# ------------------------------------------------------------------ blacklist


@pytest.mark.parametrize(
    "junk",
    ["seen live", "favorites", "favourite", "owned", "awesome", "mp3", "flac", "other"],
)
def test_scrobbling_junk_is_dropped_with_a_reason(junk: str) -> None:
    outcome = TagPolicy(genre_limit=5, style_limit=5).apply([TagInput(junk, 100)])
    assert outcome.genres == [] and outcome.tags == []
    assert outcome.dropped[0].reason == "on the blacklist"


def test_blacklist_matching_folds_case_and_whitespace() -> None:
    policy = TagPolicy(genre_limit=5, style_limit=5)
    for variant in ("Seen Live", "SEEN LIVE", "  seen   live  "):
        assert policy.apply([TagInput(variant, 1)]).genres == [], variant


def test_the_items_own_name_is_never_a_genre() -> None:
    """Tagging an artist with its own name is common and carries no information."""
    policy = TagPolicy(genre_limit=5, style_limit=5, exclude_names=("Radiohead",))
    outcome = policy.apply([TagInput("Radiohead", 900), TagInput("alternative", 800)])
    assert outcome.genres == ["alternative"]
    assert outcome.dropped[0].reason == "the item's own name"


def test_extra_blacklist_is_additive() -> None:
    policy = TagPolicy(genre_limit=5, style_limit=5, extra_blacklist=frozenset({"shoegaze"}))
    assert policy.apply([TagInput("shoegaze", 100)]).genres == []
    assert "seen live" in policy.effective_blacklist(), "the defaults still apply"


def test_default_blacklist_is_not_empty() -> None:
    assert len(DEFAULT_BLACKLIST) > 10


# --------------------------------------------------------- other rejections


def test_bare_years_are_not_genres() -> None:
    policy = TagPolicy(genre_limit=5, style_limit=5)
    for year in ("1997", "2024", "1900"):
        outcome = policy.apply([TagInput(year, 50)])
        assert outcome.genres == [], year
        assert outcome.dropped[0].reason == "a bare year"


def test_years_embedded_in_text_are_kept() -> None:
    """ "1970s" and "90s hip hop" are real genres; a bare year is not."""
    policy = TagPolicy(genre_limit=5, style_limit=5)
    outcome = policy.apply([TagInput("1970s", 50), TagInput("90s hip hop", 40)])
    assert outcome.genres == ["1970s", "90s hip hop"]


def test_urls_are_rejected() -> None:
    policy = TagPolicy(genre_limit=5, style_limit=5)
    outcome = policy.apply([TagInput("https://www.last.fm/tag/rock", 10)])
    assert outcome.genres == []
    assert outcome.dropped[0].reason == "a URL"


def test_over_long_tags_are_rejected() -> None:
    policy = TagPolicy(genre_limit=5, style_limit=5, max_tag_length=10)
    outcome = policy.apply([TagInput("a" * 11, 10), TagInput("short", 5)])
    assert outcome.genres == ["short"]
    assert "longer than" in outcome.dropped[0].reason


def test_empty_tags_are_filtered_without_being_reported_as_rejections() -> None:
    """An absent value is not a policy decision, so it must not clutter the report.

    A dropped list full of "empty" would bury the entries that explain a real
    decision, which is the whole reason the list exists.
    """
    policy = TagPolicy(genre_limit=5, style_limit=5)
    outcome = policy.apply([TagInput("", 10), TagInput("   ", 9), TagInput("pop", 5)])
    assert outcome.genres == ["pop"]
    assert outcome.dropped == [], "empty inputs are filtered, not rejected"


def test_min_count_drops_unpopular_tags() -> None:
    policy = TagPolicy(genre_limit=5, style_limit=5, min_count=50)
    outcome = policy.apply([TagInput("popular", 100), TagInput("rare", 10)])
    assert outcome.genres == ["popular"]
    assert outcome.dropped[0].reason == "count 10 below 50"


def test_min_count_does_not_drop_uncounted_tags() -> None:
    """A tag with no count is not a tag with a low count.

    ``artist.getInfo`` never returns counts, so applying ``min_count`` to those
    would silently discard every tag from that method.
    """
    policy = TagPolicy(genre_limit=5, style_limit=5, min_count=50)
    outcome = policy.apply([TagInput("nocount", None)])
    assert outcome.genres == ["nocount"]


def test_min_count_defaults_to_keeping_everything() -> None:
    outcome = TagPolicy(genre_limit=5, style_limit=5).apply([TagInput("rare", 1)])
    assert outcome.genres == ["rare"]


# ------------------------------------------------------------------ splitting


def test_multi_value_tags_are_split() -> None:
    policy = TagPolicy(genre_limit=10, style_limit=10)
    outcome = policy.apply([TagInput("Rock; Alternative", 10)])
    assert outcome.genres == ["Rock", "Alternative"]


def test_a_slash_splits_only_with_surrounding_whitespace() -> None:
    """``R&B/Soul`` is one genre; ``Rock / Indie`` is two.

    Splitting on a bare slash would turn "AC/DC" into two nonsense tags.
    """
    policy = TagPolicy(genre_limit=10, style_limit=10)
    assert policy.apply([TagInput("R&B/Soul", 10)]).genres == ["R&B/Soul"]
    assert policy.apply([TagInput("Rock / Indie", 10)]).genres == ["Rock", "Indie"]


def test_artist_names_containing_slashes_are_not_split() -> None:
    """Splitting applies to tag values, never to entity names."""
    policy = TagPolicy(genre_limit=10, style_limit=10)
    outcome = policy.apply([TagInput("ac/dc", 10)])
    assert outcome.genres == ["ac/dc"]


def test_pipe_and_semicolon_and_comma_all_split() -> None:
    policy = TagPolicy(genre_limit=10, style_limit=10)
    outcome = policy.apply([TagInput("a;b|c,d", 10)])
    assert outcome.genres == ["a", "b", "c", "d"]


# ------------------------------------------------------------------- dedupe


def test_duplicate_tags_collapse_case_insensitively() -> None:
    policy = TagPolicy(genre_limit=10, style_limit=10)
    outcome = policy.apply([TagInput("Pop", 100), TagInput("pop", 90), TagInput("POP", 80)])
    assert outcome.genres == ["Pop"], "the first spelling wins"
    assert sum(1 for drop in outcome.dropped if drop.reason == "duplicate") == 2


def test_duplicates_across_the_genre_style_boundary_collapse() -> None:
    """Otherwise one tag would be written as both a genre and a tag."""
    policy = TagPolicy(genre_limit=1, style_limit=5)
    outcome = policy.apply([TagInput("Pop", 100), TagInput("pop", 90)])
    assert outcome.genres == ["Pop"]
    assert "pop" not in outcome.tags


# --------------------------------------------------------------------- merge


def test_merge_prepends_existing_values() -> None:
    outcome = TagPolicy(genre_limit=5, style_limit=5).apply(
        [TagInput("alternative", 100)], existing_genres=["Rock"]
    )
    assert outcome.genres == ["Rock", "alternative"]


def test_merge_does_not_drop_a_curated_genre() -> None:
    """A limit bounds what Last.fm contributes, never what is already there."""
    outcome = TagPolicy(genre_limit=1, style_limit=0).apply(
        [TagInput("alternative", 100)], existing_genres=["Rock", "Pop", "Jazz"]
    )
    assert outcome.genres == ["Rock", "Pop", "Jazz", "alternative"]


def test_merge_deduplicates_existing_against_proposed() -> None:
    outcome = TagPolicy(genre_limit=5, style_limit=5).apply(
        [TagInput("Rock", 100)], existing_genres=["rock"]
    )
    assert outcome.genres == ["rock"], "the curated spelling is kept"


def test_merge_deduplicates_within_the_existing_list() -> None:
    outcome = TagPolicy(genre_limit=5, style_limit=5).apply(
        [TagInput("new", 10)], existing_genres=["Rock", "rock", "ROCK"]
    )
    assert outcome.genres == ["Rock", "new"]


def test_fill_if_empty_mode_leaves_existing_values_alone() -> None:
    policy = TagPolicy(genre_limit=5, style_limit=5, mode="fill_if_empty")
    outcome = policy.apply([TagInput("alternative", 100)], existing_genres=["Rock"])
    assert outcome.genres == ["alternative"], "no merge in this mode"


def test_replace_mode_uses_only_the_proposed_values() -> None:
    policy = TagPolicy(genre_limit=5, style_limit=5, mode="replace")
    outcome = policy.apply([TagInput("alternative", 100)], existing_genres=["Rock"])
    assert outcome.genres == ["alternative"]


def test_merge_is_the_default() -> None:
    assert TagPolicy().mode == "merge"


# ----------------------------------------------------------------------- caps


def test_max_tags_per_item_bounds_the_total() -> None:
    policy = TagPolicy(genre_limit=5, style_limit=50, max_tags_per_item=7)
    outcome = policy.apply([TagInput(f"tag{index}", 100 - index) for index in range(20)])
    assert len(outcome.genres) + len(outcome.tags) <= 7


def test_cap_applies_to_styles_after_genres() -> None:
    """Genres are the scarcer resource, so they keep their slots."""
    policy = TagPolicy(genre_limit=3, style_limit=50, max_tags_per_item=5)
    outcome = policy.apply([TagInput(f"t{index}", 100 - index) for index in range(20)])
    assert len(outcome.genres) == 3
    assert len(outcome.tags) == 2


# -------------------------------------------------------------- payload input


def test_reads_the_toptags_wire_shape_with_counts() -> None:
    tags = tags_from_payload({"tag": [{"name": "pop", "count": "100"}, {"name": "rock"}]})
    assert [(tag.name, tag.count) for tag in tags] == [("pop", 100), ("rock", None)]


def test_reads_a_bare_tag_object() -> None:
    assert names(tags_from_payload({"name": "pop"})) == ["pop"]


def test_reads_a_bare_list() -> None:
    assert names(tags_from_payload([{"name": "pop"}, {"name": "rock"}])) == ["pop", "rock"]


def test_empty_count_string_means_absent_not_zero() -> None:
    """The documented Last.fm quirk; zero would rank the tag last instead of by order."""
    assert tags_from_payload({"tag": [{"name": "pop", "count": ""}]})[0].count is None


@pytest.mark.parametrize("payload", [None, {}, [], "nonsense", 5, {"tag": None}])
def test_malformed_payloads_yield_no_tags(payload: object) -> None:
    assert tags_from_payload(payload) == []


def test_entries_without_a_name_are_skipped() -> None:
    assert names(tags_from_payload({"tag": [{"count": "5"}, {"name": ""}, {"name": "ok"}]})) == [
        "ok"
    ]


# ------------------------------------------------------------------ reporting


def test_the_summary_describes_what_happened() -> None:
    policy = TagPolicy(genre_limit=1, style_limit=1)
    outcome = policy.apply(
        [TagInput("a", 10), TagInput("b", 9), TagInput("c", 8), TagInput("seen live", 7)]
    )
    summary = outcome.summary
    assert "1 genres" in summary
    assert "dropped" in summary
    assert "over limit" in summary


def test_every_dropped_tag_has_a_reason() -> None:
    policy = TagPolicy(genre_limit=1, style_limit=1, exclude_names=("Cher",), min_count=5)
    outcome = policy.apply(
        [
            TagInput("Cher", 100),
            TagInput("seen live", 90),
            TagInput("1999", 80),
            TagInput("https://x", 70),
            TagInput("", 60),
            TagInput("rare", 1),
            TagInput("pop", 50),
        ]
    )
    assert outcome.dropped, "this input must drop something"
    assert all(drop.reason for drop in outcome.dropped)
