"""The write whitelist and editability rules (ADR 0003).

These are the highest-consequence pure functions in the codebase: getting them
wrong means silently destroying metadata on a server we do not control.
"""

from __future__ import annotations

import pytest

from metaedit.domain.writable import (
    NEVER_EDITABLE,
    core_fields,
    is_editable,
    payload_field_set,
    payload_fields,
    payload_matches_kind,
)

KINDS = ("MusicArtist", "MusicAlbum", "Audio")


@pytest.mark.parametrize("kind", KINDS)
def test_payload_fields_are_non_empty_and_unique(kind: str) -> None:
    fields = payload_fields(kind)  # type: ignore[arg-type]
    assert fields, "a payload with no fields would null the whole item"
    assert len(fields) == len(set(fields))
    assert "Name" in fields


def test_core_fields_are_identical_across_kinds() -> None:
    core = core_fields()
    for kind in KINDS:
        assert core.issubset(payload_field_set(kind))  # type: ignore[arg-type]


def test_only_artists_carry_artist_items() -> None:
    """Albums and songs must omit the NameGuidPair keys.

    Including ``ArtistItems``/``AlbumArtists`` for an album or song would feed
    Jellyfin's null-coalescing derivation and could replace existing artist links
    with nothing.
    """
    assert "ArtistItems" in payload_field_set("MusicArtist")
    for kind in ("MusicAlbum", "Audio"):
        assert "ArtistItems" not in payload_field_set(kind)  # type: ignore[arg-type]
        assert "AlbumArtists" not in payload_field_set(kind)  # type: ignore[arg-type]


def test_read_only_fields_are_never_writable() -> None:
    for kind in KINDS:
        assert payload_field_set(kind).isdisjoint(NEVER_EDITABLE)  # type: ignore[arg-type]


def test_payload_matches_kind_detects_missing_field() -> None:
    full = dict.fromkeys(payload_fields("Audio"))
    assert payload_matches_kind(full, "Audio")

    partial = dict(full)
    partial.pop("Tags")
    assert not payload_matches_kind(partial, "Audio"), (
        "a missing key is written as null by the server, so it must be rejected"
    )


def test_payload_matches_kind_detects_extra_field() -> None:
    full = dict.fromkeys(payload_fields("Audio"))
    full["AlbumArtists"] = None
    assert not payload_matches_kind(full, "Audio")


def test_payload_matches_kind_is_kind_specific() -> None:
    artist_payload = dict.fromkeys(payload_fields("MusicArtist"))
    assert payload_matches_kind(artist_payload, "MusicArtist")
    assert not payload_matches_kind(artist_payload, "Audio")


@pytest.mark.parametrize(
    ("dto", "expected"),
    [
        ({}, True),
        ({"SourceType": "Library"}, True),
        ({"SourceType": "Virtual"}, False),
        ({"LocationType": "Virtual"}, False),
        ({"Type": "CollectionFolder"}, False),
        ({"Type": "MusicAlbum"}, True),
        ({"Type": "Audio", "SourceType": "Library"}, True),
    ],
)
def test_editability_rules(dto: dict[str, object], expected: bool) -> None:
    editable, reason = is_editable(dto)
    assert editable is expected
    if not expected:
        assert reason, "a refusal must explain itself"
