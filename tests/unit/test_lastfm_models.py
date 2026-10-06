"""Last.fm response parsing.

Shapes are taken from the documented XML samples; the JSON variant differs in
small, documented ways. Every parser must tolerate what actually arrives, because
an exception here would throw away a payload we already paid for.
"""

from __future__ import annotations

from typing import Any

import pytest

from metaedit.adapters.lastfm.models import (
    parse_album,
    parse_album_search,
    parse_artist,
    parse_artist_search,
    parse_error,
    parse_similar,
    parse_top_tags,
    parse_track,
)

ARTIST_BODY: dict[str, Any] = {
    "artist": {
        "name": "Cher",
        "mbid": "bfcc6d75-a6a5-4bc6-8282-47aec8531818",
        "url": "https://www.last.fm/music/Cher",
        "image": [
            {"size": "small", "#text": "https://img/small.png"},
            {"size": "mega", "#text": "https://img/mega.png"},
        ],
        "stats": {"listeners": "196440", "plays": "1599101"},
        "similar": {
            "artist": [
                {"name": "Madonna", "mbid": "m1", "match": "1", "url": "u1"},
                {"name": "Kylie Minogue", "mbid": "", "match": "0.5432", "url": "u2"},
            ]
        },
        "tags": {"tag": [{"name": "pop", "url": "https://www.last.fm/tag/pop"}]},
        "bio": {
            "published": "Thu, 13 Mar 2008 03:59:18 +0000",
            "summary": "Cher is <b>an American</b> singer.",
            "content": "Full text",
        },
    }
}


def test_parse_artist_extracts_everything_the_mapper_needs() -> None:
    artist = parse_artist(ARTIST_BODY)
    assert artist.name == "Cher"
    assert artist.mbid == "bfcc6d75-a6a5-4bc6-8282-47aec8531818"
    assert artist.url == "https://www.last.fm/music/Cher"
    assert artist.stats is not None
    assert artist.stats.listeners == 196440
    assert artist.stats.plays == 1599101
    assert [tag.name for tag in artist.tags] == ["pop"]
    assert artist.bio is not None
    assert artist.bio.summary == "Cher is <b>an American</b> singer."
    assert [peer.name for peer in artist.similar] == ["Madonna", "Kylie Minogue"]
    assert artist.similar[0].match == 1.0
    assert artist.similar[1].match == pytest.approx(0.5432)


def test_empty_mbid_becomes_none_not_empty_string() -> None:
    """``""`` means "Last.fm has no MBID", which must not be written as a provider id."""
    artist = parse_artist(ARTIST_BODY)
    assert artist.similar[1].mbid is None


def test_image_best_prefers_the_largest_size() -> None:
    assert parse_artist(ARTIST_BODY).image.best() == "https://img/mega.png"


def test_parse_artist_tolerates_a_missing_payload() -> None:
    artist = parse_artist({})
    assert artist.name is None
    assert artist.tags == []
    assert artist.similar == []
    assert artist.bio is None


def test_parse_artist_handles_a_bare_object_where_a_list_is_expected() -> None:
    """JSON collapses single-element collections to a bare object."""
    body = {
        "artist": {
            "name": "Solo",
            "similar": {"artist": {"name": "Only One", "mbid": "x", "match": "0.9"}},
            "tags": {"tag": {"name": "pop"}},
        }
    }
    artist = parse_artist(body)
    assert [peer.name for peer in artist.similar] == ["Only One"]
    assert [tag.name for tag in artist.tags] == ["pop"]


def test_parse_top_tags_reads_the_nested_tag_list_with_counts() -> None:
    body = {
        "toptags": {
            "artist": "Cher",
            "tag": [
                {"name": "pop", "count": "100", "url": "u"},
                {"name": "dance", "count": "50", "url": "u"},
            ],
        }
    }
    top = parse_top_tags(body)
    assert [tag.name for tag in top.tags] == ["pop", "dance"]
    assert top.tags[0].count == 100


def test_parse_top_tags_treats_an_empty_count_as_absent() -> None:
    """The documented Last.fm quirk: ``"count": ""``."""
    body = {"toptags": {"tag": [{"name": "pop", "count": ""}]}}
    assert parse_top_tags(body).tags[0].count is None


def test_parse_top_tags_handles_no_tags() -> None:
    assert parse_top_tags({"toptags": {"artist": "Nobody"}}).tags == []


def test_tag_count_accepts_a_non_numeric_value() -> None:
    body = {"toptags": {"tag": [{"name": "pop", "count": "lots"}]}}
    assert parse_top_tags(body).tags[0].count is None


def test_parse_similar_uses_the_similarartists_container() -> None:
    body = {"similarartists": {"artist": [{"name": "A", "match": "0.8"}]}}
    peers = parse_similar(body)
    assert peers[0].name == "A"
    assert peers[0].match == pytest.approx(0.8)


def test_parse_similar_tolerates_an_empty_container() -> None:
    assert parse_similar({}) == []


ALBUM_BODY: dict[str, Any] = {
    "album": {
        "name": "Believe",
        "artist": "Cher",
        "id": "2026126",
        "mbid": "61bf0388-b8a9-48f4-81d1-7eb02706dfb0",
        "url": "https://www.last.fm/music/Cher/Believe",
        "releasedate": "6 Apr 1999, 00:00",
        "listeners": "47602",
        "playcount": "212991",
        "toptags": {"tag": [{"name": "pop", "url": "u"}]},
        "tracks": {
            "track": [
                {"name": "Believe", "duration": 239, "rank": "1", "mbid": ""},
                {"name": "The Power", "duration": 240, "rank": "2"},
            ]
        },
    }
}


def test_parse_album_reads_the_tracklist_and_release_date() -> None:
    album = parse_album(ALBUM_BODY)
    assert album.name == "Believe"
    assert album.artist == "Cher"
    assert album.releasedate == "6 Apr 1999, 00:00"
    assert album.listeners == 47602
    assert [tag.name for tag in album.toptags] == ["pop"]
    assert [track.name for track in album.tracks] == ["Believe", "The Power"]
    assert album.tracks[0].duration == 239, "album track durations are seconds"
    assert album.tracks[0].mbid is None


def test_parse_album_reads_the_wiki() -> None:
    """Album getInfo DOES return a wiki.

    The original version of this test asserted the opposite, and the code discarded
    the text as a result. Live verification against the real API showed a substantial
    ``wiki.summary`` on an album response, so album overviews -- one of the three
    metadata types this tool exists to edit -- were silently unavailable.
    """
    album = parse_album(
        {
            "album": {
                "name": "OK Computer",
                "artist": "Radiohead",
                "wiki": {"summary": "OK Computer is the third album.", "published": "11 Nov 2022"},
            }
        }
    )
    assert album.wiki is not None
    assert album.wiki.summary == "OK Computer is the third album."


def test_parse_album_tolerates_a_missing_wiki() -> None:
    assert parse_album({"album": {"name": "x"}}).wiki is None


def test_parse_album_reads_tags_from_the_tags_key() -> None:
    """Live-verified: album tags arrive under `tags`, not `toptags`.

    They also carry no counts, which is why tag ranking falls back to list order for
    albums rather than inventing popularity.
    """
    album = parse_album(
        {"album": {"name": "x", "tags": {"tag": [{"name": "alternative"}, {"name": "rock"}]}}}
    )
    assert [tag.name for tag in album.tags] == ["alternative", "rock"]
    assert all(tag.count is None for tag in album.tags)


def test_parse_album_tolerates_a_missing_payload() -> None:
    album = parse_album({})
    assert album.name is None
    assert album.tracks == []


TRACK_BODY: dict[str, Any] = {
    "track": {
        "id": "1019817",
        "name": "Believe",
        "mbid": "",
        "url": "https://www.last.fm/music/Cher/_/Believe",
        "duration": "240000",
        "listeners": "69572",
        "playcount": "281445",
        "artist": {"name": "Cher", "mbid": "bfcc6d75", "url": "u"},
        "album": {
            "position": "1",
            "artist": "Cher",
            "title": "Believe",
            "mbid": "61bf0388",
            "url": "u",
        },
        "toptags": {"tag": [{"name": "pop", "url": "u"}]},
        "wiki": {
            "published": "Sun, 27 Jul 2008 15:44:58 +0000",
            "summary": "A hit.",
            "content": "c",
        },
    }
}


def test_parse_track_reads_album_position_and_wiki() -> None:
    track = parse_track(TRACK_BODY)
    assert track.name == "Believe"
    assert track.duration == 240000, "track durations are milliseconds"
    assert track.artist is not None
    assert track.artist.name == "Cher"
    assert track.album is not None
    assert track.album.title == "Believe"
    assert track.album.position == 1
    assert track.wiki is not None
    assert track.wiki.summary == "A hit."
    assert track.mbid is None


def test_parse_track_tolerates_a_missing_payload() -> None:
    track = parse_track({})
    assert track.name is None
    assert track.album is None
    assert track.wiki is None


def test_parse_artist_search_reads_artistmatches() -> None:
    body = {
        "results": {
            "artistmatches": {
                "artist": [{"name": "Cher", "mbid": "m", "listeners": "5", "url": "u"}]
            }
        }
    }
    results = parse_artist_search(body)
    assert results[0].name == "Cher"
    assert results[0].listeners == 5


def test_parse_artist_search_tolerates_an_empty_result_set() -> None:
    assert parse_artist_search({"results": {}}) == []


def test_parse_album_search_reads_albummatches() -> None:
    body = {"results": {"albummatches": {"album": [{"name": "Believe", "artist": "Cher"}]}}}
    results = parse_album_search(body)
    assert results[0].name == "Believe"
    assert results[0].artist == "Cher"


def test_parse_error_extracts_code_and_message() -> None:
    assert parse_error({"error": 6, "message": "Invalid parameters"}) == (6, "Invalid parameters")


def test_parse_error_defaults_when_fields_are_missing() -> None:
    code, message = parse_error({})
    assert code == 0
    assert message
