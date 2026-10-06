"""Snapshot normalisation and payload construction (ADR 0003).

The distinguishing cases are "key absent" vs. "empty value", and the guarantee
that ``to_payload`` always emits exactly the media type's field set.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from metaedit.domain.snapshot import (
    NormalizedItemValidationError,
    from_dto,
    from_snapshot_row,
    snapshot_fields,
    to_payload,
)
from metaedit.domain.writable import payload_fields

ARTIST_DTO = {
    "Id": "11111111-1111-1111-1111-111111111111",
    "Type": "MusicArtist",
    "Name": "Radiohead",
    "Etag": "abc123",
    "DateLastSaved": "2026-01-02T03:04:05Z",
    "SourceType": "Library",
    "Genres": ["Alternative Rock"],
    "Tags": ["seen live"],
    "ProviderIds": {"MusicBrainzArtist": "a74b1b7f-71a5-4011-9441-d0b5e4122711"},
    "ExternalUrls": [{"Name": "Last.fm", "Url": "https://www.last.fm/music/Radiohead"}],
    "Overview": "English rock band.",
    "ProductionYear": 1985,
    "CommunityRating": 9,
    "LockData": False,
    "LockedFields": ["Genres"],
    "ArtistItems": [{"Name": "Radiohead", "Id": "11111111-1111-1111-1111-111111111111"}],
    "AlbumArtists": [{"Name": "Radiohead", "Id": "11111111-1111-1111-1111-111111111111"}],
}


def test_from_dto_normalises_types() -> None:
    item = from_dto(ARTIST_DTO, "MusicArtist")
    assert item.item_id == ARTIST_DTO["Id"]
    assert item.name == "Radiohead"
    assert item.etag == "abc123"
    assert item.date_last_saved == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert item.fields["CommunityRating"] == 9.0
    assert isinstance(item.fields["CommunityRating"], float)
    assert item.artist_names == ["Radiohead"]
    assert item.album_artist_names == ["Radiohead"]


def test_missing_key_is_filled_with_its_empty_value() -> None:
    """A key the server did not send becomes that field's empty value.

    A complete snapshot is what makes ``to_payload`` safe: every field the media
    type requires is always present, so a short body can never be produced.
    """
    dto = {key: value for key, value in ARTIST_DTO.items() if key != "Tags"}
    item = from_dto(dto, "MusicArtist")
    assert item.fields["Tags"] == []

    # ...and a value the server *did* send is preserved as-is.
    empty_sent = from_dto({**dto, "Tags": []}, "MusicArtist")
    assert empty_sent.fields["Tags"] == []

    # Dict-shaped fields default to an empty object, not a list.
    without_ids = {key: value for key, value in dto.items() if key != "ProviderIds"}
    assert from_dto(without_ids, "MusicArtist").fields["ProviderIds"] == {}


def test_to_payload_is_always_complete_even_when_the_read_was_short() -> None:
    dto = {key: value for key, value in ARTIST_DTO.items() if key != "Tags"}
    item = from_dto(dto, "MusicArtist")
    payload = to_payload(item, {})
    assert set(payload) == set(payload_fields("MusicArtist"))
    # The field the read did not supply carries the empty value, never a hole.
    assert payload["Tags"] == []
    assert payload["Genres"] == ["Alternative Rock"]
    assert payload["Name"] == "Radiohead"


def test_to_payload_layers_changes_over_current_values() -> None:
    item = from_dto(ARTIST_DTO, "MusicArtist")
    payload = to_payload(item, {"Genres": ["Art Rock", "Alternative Rock"]})
    assert payload["Genres"] == ["Art Rock", "Alternative Rock"]
    # Untouched fields keep their existing values.
    assert payload["Overview"] == "English rock band."
    assert payload["LockedFields"] == ["Genres"]


def test_to_payload_emits_the_complete_field_set() -> None:
    item = from_dto(ARTIST_DTO, "MusicArtist")
    payload = to_payload(item, {"Tags": ["art rock"]})
    assert set(payload) == set(payload_fields("MusicArtist"))


def test_to_payload_only_ever_emits_the_field_set_for_its_kind() -> None:
    """A hand-edited snapshot cannot smuggle in a foreign key.

    ``to_payload`` builds from ``payload_fields(kind)``, so an entry such as
    ``ArtistItems`` on a song is dropped rather than sent.
    """
    item = from_dto(ARTIST_DTO, "Audio")
    smuggled = item.model_copy(update={"fields": {**item.fields, "ArtistItems": None}})
    payload = to_payload(smuggled, {})
    assert set(payload) == set(payload_fields("Audio"))
    assert "ArtistItems" not in payload


def test_to_payload_guard_is_effective(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The completeness guard is defensive and must actually be able to fire.

    By construction ``to_payload`` cannot build a short body today, so the guard
    is exercised by simulating the refactor that would break it: a field set that
    omits something. That makes the guard a tested invariant rather than a
    decorative ``assert``.
    """
    from metaedit.domain import snapshot as snapshot_module
    from metaedit.domain.writable import payload_fields as real_payload_fields

    item = from_dto(ARTIST_DTO, "MusicArtist")

    def short_payload_fields(kind: str) -> tuple[str, ...]:
        return tuple(f for f in real_payload_fields(kind) if f != "Tags")  # type: ignore[arg-type]

    monkeypatch.setattr(snapshot_module, "payload_fields", short_payload_fields)
    # Both sides of the comparison use the patched function, so simulate the
    # mismatch the guard is for by also patching the checker.
    monkeypatch.setattr(
        snapshot_module,
        "payload_matches_kind",
        lambda payload, kind: payload.get("__never__") is not None,
    )
    with pytest.raises(NormalizedItemValidationError, match="refusing to send an incomplete"):
        to_payload(item, {})


def test_to_payload_converts_datetime_to_iso() -> None:
    item = from_dto(ARTIST_DTO, "MusicArtist")
    payload = to_payload(item, {"PremiereDate": datetime(1995, 3, 13, tzinfo=UTC)})
    assert payload["PremiereDate"] == "1995-03-13T00:00:00+00:00"


def test_snapshot_fields_matches_the_payload_field_set() -> None:
    item = from_dto(ARTIST_DTO, "MusicArtist")
    stored = snapshot_fields(item)
    assert set(stored).issubset(set(payload_fields("MusicArtist")))
    assert stored["Genres"] == ["Alternative Rock"]


def test_snapshot_round_trip_reproduces_the_payload() -> None:
    """A revert must send exactly what was there before the write."""
    item = from_dto(ARTIST_DTO, "MusicArtist")
    original_payload = to_payload(item, {})

    row = {
        "kind": "MusicArtist",
        "item_id": item.item_id,
        "name": item.name,
        "etag": item.etag,
        "date_last_saved": item.date_last_saved,
        "fields": snapshot_fields(item),
    }
    restored = from_snapshot_row(row)
    assert to_payload(restored, {}) == original_payload


def test_album_and_song_snapshots_exclude_artist_items() -> None:
    album = {
        "Id": "22222222-2222-2222-2222-222222222222",
        "Type": "MusicAlbum",
        "Name": "OK Computer",
        "Genres": ["Alternative Rock"],
        "AlbumArtists": [{"Name": "Radiohead", "Id": None}],
    }
    item = from_dto(album, "MusicAlbum")
    assert "ArtistItems" not in item.fields
    assert item.album_artist_names == ["Radiohead"]
    payload = to_payload(item, {})
    assert "ArtistItems" not in payload
    assert "AlbumArtists" not in payload


def test_as_state_exposes_lock_state_and_editability() -> None:
    state = from_dto(ARTIST_DTO, "MusicArtist").as_state()
    assert state["locked"] is False
    assert state["locked_fields"] == ["Genres"]
    assert state["editable"] is True
    assert state["etag"] == "abc123"
    assert state["date_last_saved"] == "2026-01-02T03:04:05+00:00"


def test_virtual_item_is_not_editable() -> None:
    dto = {**ARTIST_DTO, "SourceType": "Virtual"}
    state = from_dto(dto, "MusicArtist").as_state()
    assert state["editable"] is False
    assert state["not_editable_reason"]


def test_unknown_keys_do_not_leak_into_the_payload() -> None:
    dto = {**ARTIST_DTO, "MediaSources": [{"Path": "/music"}], "Trickplay": {"x": 1}}
    item = from_dto(dto, "MusicArtist")
    assert "MediaSources" not in item.fields
    assert "Trickplay" not in item.fields


def test_snapshot_row_rebuild_tolerates_missing_and_extra_entries() -> None:
    """Reverting a snapshot written before a field existed must still work."""
    restored = from_snapshot_row(
        {
            "kind": "MusicArtist",
            "item_id": "abc",
            "name": "Radiohead",
            "fields": {"Genres": ["Art Rock"], "RetiredField": "ignore me"},
        }
    )
    payload = to_payload(restored, {})
    assert set(payload) == set(payload_fields("MusicArtist"))
    assert payload["Genres"] == ["Art Rock"]
    assert payload["Tags"] == []
