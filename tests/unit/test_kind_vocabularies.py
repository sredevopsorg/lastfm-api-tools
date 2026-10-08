"""The two kind vocabularies must stay deliberately different, and consistently so.

There are two names for one thing, and they are not interchangeable:

* the **archive** vocabulary — ``artist`` / ``album`` / ``track`` — used by the derived
  layer (``lastfm_track``, ``ReindexKind``). Its reader is someone writing SQL.
* the **query** vocabulary — ``artist`` / ``album`` / ``song`` — used by every surface a
  person or the SPA touches: the library browse, the bulk selection, the harvest
  selection. Its reader is someone picking from a list.

The bulk editor took its vocabulary from one and its lookup from the other, so every
bulk diff over songs was rejected with ``unknown selection kind 'song'`` while the UI
cheerfully offered "Songs" as an option. Nothing caught it: the schema accepted the
value, and the service's map had no reason to know the schema existed.

These tests pin the relationship rather than the values, so adding a media type means
updating one mapping and being told about every other place that has to agree.
"""

from __future__ import annotations

from typing import get_args

import pytest

from metaedit.api.bulk import SelectionKind
from metaedit.api.library import KindParam
from metaedit.domain.writable import ITEM_KINDS_ALL
from metaedit.service.bulk import ITEM_KIND_BY_QUERY
from metaedit.service.planning import ENTITY_KIND_BY_ITEM_KIND, QUERY_KIND_BY_ITEM_KIND


def test_every_media_type_has_both_a_query_kind_and_an_archive_kind() -> None:
    for kind in ITEM_KINDS_ALL:
        assert kind in QUERY_KIND_BY_ITEM_KIND, f"{kind} has no query kind"
        assert kind in ENTITY_KIND_BY_ITEM_KIND, f"{kind} has no archive kind"


def test_the_query_vocabulary_says_song_and_the_archive_vocabulary_says_track() -> None:
    """The mismatch was the bug; the mismatch is also the intent."""
    assert QUERY_KIND_BY_ITEM_KIND["Audio"] == "song"
    assert ENTITY_KIND_BY_ITEM_KIND["Audio"] == "track"


def test_a_song_is_selectable_by_the_name_the_ui_shows() -> None:
    assert ITEM_KIND_BY_QUERY["song"] == "Audio"


@pytest.mark.parametrize("kind", ITEM_KINDS_ALL)
def test_every_media_type_is_reachable_through_the_browse_map(kind: str) -> None:
    """The registry both endpoints read; a missing entry is a rejected request."""
    assert QUERY_KIND_BY_ITEM_KIND[kind] in ITEM_KIND_BY_QUERY


def test_the_api_surfaces_accept_exactly_the_query_vocabulary() -> None:
    """The three `kind` enums are the same set, and it is the query one.

    `SelectionKind` (bulk) and `KindParam` (library) are literals written out for the
    schema; harvest uses the same three. If someone adds a fourth query kind to one of
    them and not the others, this fails rather than the request failing at runtime.
    """
    expected = set(QUERY_KIND_BY_ITEM_KIND.values())
    assert set(get_args(SelectionKind)) == expected
    assert set(get_args(KindParam)) == expected
    assert set(ITEM_KIND_BY_QUERY) == expected


def test_the_archive_vocabulary_is_not_accepted_as_a_query_kind() -> None:
    """Guards the other direction: `kind=track` is not a browse kind."""
    archive_only = set(ENTITY_KIND_BY_ITEM_KIND.values()) - set(QUERY_KIND_BY_ITEM_KIND.values())
    assert archive_only == {"track"}
    for kind in archive_only:
        assert kind not in ITEM_KIND_BY_QUERY
