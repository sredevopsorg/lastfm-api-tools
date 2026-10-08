"""The composition of the narrowings a selection applies.

The four callers of this module -- library browse, batch diff, genre removal, harvest --
each spend a Jellyfin request per item they touch, and two of them write. So the assertions
that matter here are about the *composition*: which of the two predicates decides whether
an item is kept, and which counter each dropped item lands in.
"""

from __future__ import annotations

from typing import Any

import pytest

from metaedit.adapters.jellyfin.dto import BaseItemDto, BaseItemDtoQueryResult
from metaedit.domain.errors import ValidationError
from metaedit.service import selection as selection_service
from metaedit.service.selection import Selection, SelectionOutcome, collect


def _dto(
    item_id: str,
    name: str,
    *,
    kind: str = "Audio",
    genres: list[str] | None = None,
    album: str | None = None,
    album_artist: str | None = None,
) -> BaseItemDto:
    payload: dict[str, Any] = {
        "Id": item_id,
        "Type": kind,
        "Name": name,
        "Genres": genres or [],
        "Tags": [],
        "ProviderIds": {},
        "Overview": "",
    }
    if album:
        payload["Album"] = album
    if album_artist:
        payload["AlbumArtist"] = album_artist
    return BaseItemDto.model_validate(payload)


class FakeClient:
    """Just the two methods ``collect`` calls, recording what it was asked for."""

    def __init__(self, rows: list[BaseItemDto]) -> None:
        self.rows = rows
        self.queries: list[dict[str, Any]] = []
        self.hydrations: list[list[str]] = []

    async def items(self, **kwargs: Any) -> BaseItemDtoQueryResult:
        self.queries.append(kwargs)
        rows = list(self.rows)
        start = kwargs.get("start_index") or 0
        limit = kwargs.get("limit") or len(rows)
        return BaseItemDtoQueryResult(Items=rows[start : start + limit], TotalRecordCount=len(rows))

    async def items_by_ids(self, item_ids: list[str], **_: Any) -> list[BaseItemDto]:
        self.hydrations.append(list(item_ids))
        by_id = {row.Id: row for row in self.rows}
        return [by_id[item_id] for item_id in item_ids if item_id in by_id]


# ------------------------------------------------------------------ building


def test_a_bare_pattern_is_compiled_and_an_id_is_shape_checked() -> None:
    built = Selection.build(
        kind="song",
        artist_ids=["a" * 32],
        exclude=["*Live*"],
    )
    assert built.patterns == ("*live*",)
    assert built.jellyfin_params() == {"ArtistIds": "a" * 32}


def test_an_unparseable_id_is_refused_when_the_selection_is_built() -> None:
    # Refused here rather than at the request, because Jellyfin discards an unparseable
    # list in full and answers with the unfiltered library -- which on a write path means a
    # library-wide batch built from a typo.
    with pytest.raises(ValidationError):
        Selection.build(kind="song", artist_ids=["abc"])


def test_a_facet_that_cannot_apply_is_refused_not_answered_with_zero() -> None:
    with pytest.raises(ValidationError):
        Selection.build(kind="artist", artist_ids=["a" * 32]).jellyfin_params()


def test_aspects_are_normalised_against_the_media_type() -> None:
    """Asking a song library for "missing overview" must select nothing, not everything.

    Measured on a live library: 5,441 of 5,442 songs have no overview, so treating it as a
    gap would make the filter indistinguishable from no filter.
    """
    song = Selection.build(kind="song", missing=["overview"])
    assert song.aspects == frozenset()
    assert song.needs_read is False

    artist = Selection.build(kind="artist", missing=["overview"])
    assert artist.aspects == frozenset({"overview"})
    assert artist.needs_read is True


def test_only_a_read_filter_makes_a_selection_need_a_read() -> None:
    # The distinction the browse screen also draws: Jellyfin answers the first two itself.
    assert Selection.build(kind="song", artist_ids=["a" * 32]).needs_read is False
    assert Selection.build(kind="song", exclude=["*live*"]).needs_read is True
    assert Selection.build(kind="song", missing=["genres"]).needs_read is True


# ------------------------------------------------------------------ predicates


def test_no_aspect_filter_keeps_everything_that_is_not_excluded() -> None:
    """The composition bug this pins cost a debugging session.

    ``is_missing`` answers *False* when no aspect was asked for -- nothing is missing from
    a filter that asks for nothing -- so reading it as the keep test made an
    exclusion-only selection match nothing at all.
    """
    selection = Selection.build(kind="song", exclude=["*live*"])

    assert selection.selected_by_aspects(_dto("a" * 32, "Studio Version")) is True
    assert selection.carries(_dto("a" * 32, "Studio Version")) is True
    assert selection.carries(_dto("b" * 32, "Live Version")) is False


def test_a_pattern_sees_the_album_and_the_album_artist() -> None:
    selection = Selection.build(kind="song", exclude=["Various Artists", "*live*"])

    assert selection.carries(_dto("a" * 32, "Track", album_artist="Various Artists")) is False
    assert selection.carries(_dto("b" * 32, "Track", album="Live at Leeds")) is False
    assert selection.carries(_dto("c" * 32, "Track", album="OK Computer")) is True


def test_both_filters_must_pass() -> None:
    selection = Selection.build(kind="artist", missing=["genres"], exclude=["*tribute*"])

    keeps = _dto("a" * 32, "Nobody", kind="MusicArtist", genres=[])
    has_genres = _dto("b" * 32, "Nobody", kind="MusicArtist", genres=["Rock"])
    excluded = _dto("c" * 32, "A Tribute Band", kind="MusicArtist", genres=[])

    assert selection.carries(keeps) is True
    assert selection.carries(has_genres) is False, "it already has genres"
    assert selection.carries(excluded) is False


# ------------------------------------------------------------------ collecting


@pytest.mark.asyncio
async def test_a_forwarded_selection_reads_nothing_and_says_so() -> None:
    client = FakeClient([_dto(f"{index:032x}", f"Song {index}") for index in range(5)])

    outcome = await collect(
        client,
        selection=Selection.build(kind="song"),
        limit=2,  # type: ignore[arg-type]
    )

    assert len(outcome.items) == 2
    assert outcome.scanned is None, "nothing was read here; Jellyfin answered the question"
    assert len(client.queries) == 1
    assert client.queries[0]["filters"] == {}


@pytest.mark.asyncio
async def test_explicit_ids_are_filtered_and_the_drops_are_counted() -> None:
    """The caller named the items, so nothing is over-fetched -- but a short result is
    still explained rather than left to be guessed at."""
    rows = [
        _dto("a" * 32, "Studio"),
        _dto("b" * 32, "Live at Leeds", album_artist="Various Artists"),
    ]
    client = FakeClient(rows)
    selection = Selection.build(kind="song", exclude=["VariOus ArtiSts"])

    outcome = await collect(
        client,
        selection=selection,
        ids=[row.Id for row in rows],
        limit=10,  # type: ignore[arg-type]
    )

    assert [item.Id for item in outcome.items] == ["a" * 32]
    assert outcome.scanned == 2
    assert outcome.excluded == 1
    assert client.queries == [], "an explicit id list must not trigger a browse"


@pytest.mark.asyncio
async def test_a_read_selection_over_fetches_to_fill_the_limit() -> None:
    """Filtering one page and then dropping items would return a short batch while
    perfectly good candidates sat unread -- the "quietly too short" defect."""
    rows = [_dto(f"{index:032x}", f"Live {index}") for index in range(4)] + [
        _dto("f" * 32, "Studio")
    ]
    client = FakeClient(rows)

    outcome = await collect(
        client,
        selection=Selection.build(kind="song", exclude=["*live*"]),  # type: ignore[arg-type]
        limit=1,
    )

    assert [item.Name for item in outcome.items] == ["Studio"]
    assert outcome.excluded == 4
    assert outcome.scanned == 5
    assert outcome.truncated is False, "the library ended; nothing was left unread"


@pytest.mark.asyncio
async def test_the_report_is_not_falsy_when_it_read_items_and_excluded_all_of_them() -> None:
    """A report's *emptiness* and its *existence* are different questions.

    ``SelectionOutcome`` once defined ``__bool__`` meaning "found some items", so
    ``if report:`` -- the obvious test for a report being present -- took the absent branch
    for exactly this case, and the counters then read as zero. A batch that had dropped
    everything reported dropping nothing.
    """
    client = FakeClient([_dto("a" * 32, "Live at Leeds")])

    outcome = await collect(
        client,
        selection=Selection.build(kind="song", exclude=["*live*"]),  # type: ignore[arg-type]
        limit=5,
    )

    assert outcome.items == []
    assert outcome.excluded == 1
    assert bool(outcome) is True, "an empty report is still a report"
    assert selection_service.SelectionOutcome(items=[], excluded=7).excluded == 7


@pytest.mark.asyncio
async def test_a_capped_read_says_it_is_incomplete() -> None:
    client = FakeClient([_dto(f"{index:032x}", f"Song {index}") for index in range(10)])

    outcome = await collect(
        client,
        selection=Selection.build(kind="song", missing=["genres"]),  # type: ignore[arg-type]
        limit=10,
        scan_cap=3,
    )

    assert outcome.truncated is True
    assert outcome.scanned == 3


def test_the_outcome_has_no_dunder_truthiness() -> None:
    """Structural, because the bug it caused was silent.

    ``__bool__``/``__len__`` on the outcome makes ``if outcome:`` mean "found something"
    rather than "exists", and every ``if report:`` in the summary code then reads the
    counters as zero. Stated as a test so re-adding one is a decision, not a convenience.
    """
    assert not hasattr(SelectionOutcome, "__bool__")
    assert not hasattr(SelectionOutcome, "__len__")
