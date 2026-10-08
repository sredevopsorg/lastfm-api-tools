"""Which aspects count as "missing", and for which media type.

This module exists because the previous boolean filter had no notion of media type, and
that made it useless for songs. Measured on a live library:

    MusicArtist  500 items: overview present in 254, provider ids in 414, genres in 392
    MusicAlbum   500 items: overview present in 113, provider ids in 397, genres in 441
    Audio       500 items: overview present in   1, provider ids in  69, genres in 314

A song with no ``Overview`` is the normal state of a song. Counting it as "missing
metadata" marked 5,441 of 5,442 songs incomplete, which is not a filter -- it is a wall
of red that hides the 1,466 songs whose missing genres are actually fixable.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from metaedit.domain.browse_filters import (
    ASPECTS_BY_KIND,
    MAX_SCAN_ITEMS,
    is_missing,
    normalise_aspects,
)
from metaedit.domain.writable import ITEM_KINDS_ALL


def item(
    *,
    genres: list[str] | None = None,
    tags: list[str] | None = None,
    has_provider_ids: bool = True,
    has_overview: bool = True,
) -> SimpleNamespace:
    return SimpleNamespace(
        genres=genres or [],
        tags=tags or [],
        has_provider_ids=has_provider_ids,
        has_overview=has_overview,
    )


def test_every_media_type_has_a_definition() -> None:
    """A kind with no entry would KeyError on a browse, not fall back to anything."""
    for kind in ITEM_KINDS_ALL:
        assert kind in ASPECTS_BY_KIND


def test_songs_are_not_filtered_on_missing_overviews() -> None:
    """The measurement above, as an assertion.

    This is the single most consequential line in the module: including `overview` for
    songs turns the filter into a match-everything.
    """
    assert "overview" not in ASPECTS_BY_KIND["Audio"]
    assert "overview" in ASPECTS_BY_KIND["MusicArtist"]
    assert "overview" in ASPECTS_BY_KIND["MusicAlbum"]


def test_the_aspects_songs_are_judged_on_are_the_ones_that_get_fixed() -> None:
    assert ASPECTS_BY_KIND["Audio"] == frozenset({"genres", "provider_ids", "tags"})


def test_a_song_with_no_overview_is_not_reported_as_missing() -> None:
    """The concrete consequence: this item must not appear in a missing-metadata browse.

    Note the tags: an empty tag list is itself something a song can be missing, so a
    song is "complete" here only by having everything *it* is judged on. That asymmetry
    is the point -- the same item is missing for an artist, because an artist is judged
    on its overview too.
    """
    song = item(genres=["Rock"], tags=["britpop"], has_overview=False)
    assert is_missing("Audio", ASPECTS_BY_KIND["Audio"], song) is False


def test_the_same_item_is_missing_for_an_artist() -> None:
    """The definition is per media type, not global -- that is the whole design."""
    artist = item(genres=["Rock"], has_overview=False)
    assert is_missing("MusicArtist", ASPECTS_BY_KIND["MusicArtist"], artist) is True


@pytest.mark.parametrize(
    ("aspect", "kwargs"),
    [
        ("genres", {"genres": []}),
        ("tags", {"tags": []}),
        ("provider_ids", {"has_provider_ids": False}),
        ("overview", {"has_overview": False}),
    ],
)
def test_each_aspect_detects_its_own_absence(aspect: str, kwargs: dict[str, object]) -> None:
    assert is_missing("MusicArtist", frozenset({aspect}), item(**kwargs)) is True  # type: ignore[arg-type]


def test_a_complete_item_is_missing_nothing() -> None:
    complete = item(genres=["Rock"], tags=["britpop"])
    assert is_missing("MusicArtist", ASPECTS_BY_KIND["MusicArtist"], complete) is False


def test_any_missing_aspect_is_enough() -> None:
    """A filter asks what is worth looking at, not what is comprehensively lacking."""
    has_genres_only = item(genres=["Rock"], has_provider_ids=False, has_overview=False)
    assert is_missing("MusicArtist", ASPECTS_BY_KIND["MusicArtist"], has_genres_only) is True


def test_no_aspects_means_no_filter_not_everything() -> None:
    """An empty filter must not match every item, or an empty query would hide the list."""
    assert is_missing("MusicArtist", frozenset(), item()) is False


def test_an_aspect_that_does_not_apply_is_dropped_not_honoured() -> None:
    """Asking a song library for missing overviews must not mark every song."""
    assert normalise_aspects("Audio", frozenset({"overview"})) == frozenset()
    assert normalise_aspects("Audio", frozenset({"overview", "genres"})) == frozenset({"genres"})


def test_applicable_aspects_survive_normalisation() -> None:
    requested = frozenset({"genres", "provider_ids"})
    assert normalise_aspects("Audio", requested) == requested


def test_an_unknown_aspect_is_dropped_rather_than_crashing() -> None:
    assert normalise_aspects("MusicArtist", frozenset({"not_a_field"})) == frozenset()


def test_the_scan_cap_is_finite_and_reported() -> None:
    """A cap of None would be an unbounded read on a 5,442-song library."""
    assert isinstance(MAX_SCAN_ITEMS, int)
    assert 0 < MAX_SCAN_ITEMS < 100_000


def test_the_bulk_service_uses_this_same_definition() -> None:
    """One definition, or the browse and the bulk editor disagree about what needs work.

    The bulk selection and the harvest selection had their own inline test -- "not
    Genres or not ProviderIds or not Overview" -- which was the same conflation this
    module exists to remove, left behind when the library browse was fixed. Over a song
    library that matched almost every item, on a screen whose output is a write.

    Asserted structurally rather than by re-testing the logic: the service must read the
    domain's predicate, and a second inline implementation is what this catches.
    """
    import inspect

    from metaedit.service import bulk as bulk_service

    source = inspect.getsource(bulk_service.select_items)
    assert "is_missing(" in source, "the bulk selection must use the shared predicate"
    assert "not item.Genres" not in source, "an inline missing-test has crept back in"


def test_the_adapter_presents_a_jellyfin_dto_in_the_shape_the_filter_reads() -> None:
    """The bridge between an adapter DTO and this domain predicate.

    Two implementations of "has no genres" is exactly how the browse and the bulk editor
    would come to disagree, so there is one predicate and a small adapter.
    """
    from metaedit.adapters.jellyfin.dto import BaseItemDto
    from metaedit.service.bulk import _aspects_of

    dto = BaseItemDto.model_validate(
        {
            "Id": "x",
            "Name": "A Song",
            "Type": "Audio",
            "Genres": [],
            "Tags": ["keep"],
            "ProviderIds": {},
            "Overview": "",
        }
    )
    aspects = _aspects_of(dto)
    assert aspects.genres == []
    assert aspects.tags == ["keep"]
    assert aspects.has_provider_ids is False
    assert aspects.has_overview is False
    # And the predicate accepts it, which is the point of the Protocol.
    assert is_missing("Audio", frozenset({"genres"}), aspects) is True
    assert is_missing("Audio", frozenset({"overview"}), aspects) is False, "songs ignore it"
