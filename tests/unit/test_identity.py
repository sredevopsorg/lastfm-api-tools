"""Entity identity keys (docs/design/0003 §2).

The escaping exists because Last.fm names and tags are user-generated, so a
delimiter character can legitimately appear in one. Without it, two unrelated
entities collide on the same derived primary key and silently merge.
"""

from __future__ import annotations

import pytest

from metaedit.archive.identity import (
    FIELD_SEPARATOR,
    album_identity,
    artist_identity,
    candidate_keys,
    escape,
    is_mbid_key,
    key_for,
    track_identity,
    unescape,
)


def test_mbid_wins_when_present() -> None:
    assert artist_identity(mbid="abc", name="Cher") == "mbid:abc"
    assert album_identity(mbid="abc", artist="Cher", name="Believe") == "mbid:abc"
    assert track_identity(mbid="abc", artist="Cher", name="Believe") == "mbid:abc"


def test_name_key_is_normalised() -> None:
    assert artist_identity(mbid=None, name="  Cher   Cher ") == "name:Cher Cher"
    # Nothing to key on is an empty identity, not a "name:" row anyone could occupy.
    assert artist_identity(mbid=None, name=None) == ""


def test_case_is_not_folded() -> None:
    """Last.fm echoes a canonical spelling; folding case would merge entities."""
    assert artist_identity(mbid=None, name="cher") != artist_identity(mbid=None, name="Cher")


def test_composite_key_uses_a_delimiter() -> None:
    key = album_identity(mbid=None, artist="Cher", name="Believe")
    assert key == f"album:Cher{FIELD_SEPARATOR}Believe"


def test_unrelated_entities_cannot_collide_on_the_delimiter() -> None:
    """The regression this escaping exists for.

    An artist literally named ``A<US>B`` with an empty album name must not produce
    the same key as the artist ``A`` with album ``B``, or two unrelated albums would
    share one derived row.
    """
    forged = album_identity(mbid=None, artist=f"A{FIELD_SEPARATOR}B", name="x")
    pair = album_identity(mbid=None, artist="A", name=f"B{FIELD_SEPARATOR}x")
    assert forged != pair
    # And the delimiter stays a literal separator when no component contains one.
    assert album_identity(mbid=None, artist="A", name="B") == f"album:A{FIELD_SEPARATOR}B"


def test_percent_cannot_forge_the_escape_sequence() -> None:
    """``%`` is encoded first, so the mapping stays injective."""
    assert escape("%1F") != escape(FIELD_SEPARATOR)
    assert album_identity(mbid=None, artist="A%1FB", name="x") != album_identity(
        mbid=None, artist=f"A{FIELD_SEPARATOR}B", name="x"
    )
    assert album_identity(mbid=None, artist="100%", name="x") != album_identity(
        mbid=None, artist="100", name="%x"
    )


def test_escape_round_trips() -> None:
    for value in ("plain", f"with{FIELD_SEPARATOR}sep", "100%", f"%%{FIELD_SEPARATOR}"):
        assert unescape(escape(value)) == value


def test_escaping_is_deterministic() -> None:
    assert escape(f"a{FIELD_SEPARATOR}b") == escape(f"a{FIELD_SEPARATOR}b")


def test_album_and_track_keys_do_not_share_a_namespace() -> None:
    """An album and a track with identical names are different entities."""
    album = album_identity(mbid=None, artist="Cher", name="Believe")
    track = track_identity(mbid=None, artist="Cher", name="Believe")
    assert album != track
    assert album.startswith("album:")
    assert track.startswith("track:")


def test_key_for_dispatches_by_kind() -> None:
    assert key_for("artist", mbid=None, artist=None, name="Cher") == "name:Cher"
    assert key_for("album", mbid=None, artist="Cher", name="Believe").startswith("album:")
    assert key_for("track", mbid=None, artist="Cher", name="Believe").startswith("track:")


def test_candidate_keys_include_the_name_form_for_an_mbid_key() -> None:
    """An entity that gains an MBID must still be recognisable by its old name key."""
    keys = candidate_keys("artist", mbid="m1", artist=None, name="Cher")
    assert keys == {"mbid:m1", "name:Cher"}


def test_candidate_keys_for_a_name_only_entity_are_just_the_name() -> None:
    assert candidate_keys("artist", mbid=None, artist=None, name="Cher") == {"name:Cher"}


def test_an_unidentifiable_entity_has_no_key_at_all() -> None:
    """A degenerate ``"name:"`` key would collide every unnamed entity into one row."""
    assert artist_identity(mbid=None, name=None) == ""
    assert artist_identity(mbid=None, name="   ") == ""
    assert album_identity(mbid=None, artist="Cher", name=None) == ""
    assert track_identity(mbid=None, artist=None, name="") == ""
    assert candidate_keys("artist", mbid=None, artist=None, name=None) == set()


def test_an_mbid_alone_is_enough_to_identify() -> None:
    assert artist_identity(mbid="m1", name=None) == "mbid:m1"


def test_a_name_alone_is_enough_to_identify() -> None:
    assert artist_identity(mbid=None, name="Cher") == "name:Cher"


def test_candidate_keys_for_an_album_include_artist_and_name() -> None:
    keys = candidate_keys("album", mbid="m1", artist="Cher", name="Believe")
    assert f"album:Cher{FIELD_SEPARATOR}Believe" in keys
    assert "mbid:m1" in keys


@pytest.mark.parametrize("kind", ["artist", "album", "track"])
def test_identities_are_stable_for_the_same_input(kind: str) -> None:
    first = key_for(kind, mbid=None, artist="Sigur Rós", name="Ágætis byrjun")  # type: ignore[arg-type]
    second = key_for(kind, mbid=None, artist="Sigur Rós", name="Ágætis byrjun")  # type: ignore[arg-type]
    assert first == second
    # NFC/NFD must fold, or the same name would key two ways.
    import unicodedata

    nfd = unicodedata.normalize("NFD", "Sigur Rós")
    assert key_for(kind, mbid=None, artist=nfd, name="x") == key_for(  # type: ignore[arg-type]
        kind,
        mbid=None,
        artist="Sigur Rós",
        name="x",  # type: ignore[arg-type]
    )


def test_is_mbid_key_distinguishes_forms() -> None:
    assert is_mbid_key("mbid:abc")
    assert not is_mbid_key("name:Cher")
    assert not is_mbid_key("album:A\x1fB")
