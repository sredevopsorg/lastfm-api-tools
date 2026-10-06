"""The one list of Jellyfin fields this application is allowed to write.

``POST /Items/{itemId}`` is a full overwrite, not a patch: every field the server
reads from the request body is assigned unconditionally, so a key missing from
the body is written as null/empty (ADR 0003). Therefore:

    the write payload must contain exactly ``payload_fields(kind)`` -- no more,
    no fewer.

``ArtistItems``/``AlbumArtists`` are handled specially. They feed Jellyfin's
internal ``Artists``/``AlbumArtists`` arrays through a write-if-null derivation,
so **omitting** the key is what preserves existing artist links. Media types
differ in which of the two they even have: albums and songs have no artist link
list to rewrite.
"""

from __future__ import annotations

from typing import Any, Literal

ItemKind = Literal["MusicArtist", "MusicAlbum", "Audio"]

# The media types this application edits, in a stable order.
ITEM_KINDS_ALL: tuple[ItemKind, ...] = ("MusicArtist", "MusicAlbum", "Audio")

# Plain scalar/collection fields the item update path assigns unconditionally.
_CORE_WRITABLE_FIELDS: tuple[str, ...] = (
    "Name",
    "ForcedSortName",
    "OriginalTitle",
    "Overview",
    "Genres",
    "Tags",
    "Studios",
    "ProductionLocations",
    "ProviderIds",
    "ExternalUrls",
    "CommunityRating",
    "CriticRating",
    "PremiereDate",
    "ProductionYear",
    "OfficialRating",
    "CustomRating",
    "PreferredMetadataLanguage",
    "PreferredMetadataCountryCode",
    "People",
    "LockData",
    "LockedFields",
)

# Fields whose subset included in a payload depends on the media type.
_ARTIST_ITEM_FIELD = "ArtistItems"
_ALBUM_ARTIST_FIELD = "AlbumArtists"

# Fields that cannot be edited here and must never be mistaken for changes.
NEVER_EDITABLE = frozenset({"Id", "Etag", "Type", "MediaType", "SourceType", "Path"})

# Collections whose items are never directly editable.
NON_EDITABLE_SOURCE_TYPES = frozenset({"Virtual"})
NON_EDITABLE_TYPES = frozenset(
    {"CollectionFolder", "UserView", "Folder", "AggregateFolder", "MusicGenre", "Genre"}
)


def payload_fields(kind: ItemKind) -> tuple[str, ...]:
    """The exact key set a write payload for ``kind`` must have."""
    if kind == "MusicArtist":
        # An artist owns its own artist links.
        return (*_CORE_WRITABLE_FIELDS, _ARTIST_ITEM_FIELD)
    # Albums and songs: both NameGuidPair keys are omitted so the server's
    # write-if-null derivation cannot replace existing artist links with nothing.
    return _CORE_WRITABLE_FIELDS


def payload_field_set(kind: ItemKind) -> frozenset[str]:
    return frozenset(payload_fields(kind))


def core_fields() -> frozenset[str]:
    """Fields that mean the same thing for every media type."""
    return frozenset(_CORE_WRITABLE_FIELDS)


def is_editable(dto: dict[str, Any]) -> tuple[bool, str | None]:
    """Whether an item may be edited, with a human-readable reason if not."""
    source_type = dto.get("SourceType")
    if source_type is not None and source_type in NON_EDITABLE_SOURCE_TYPES:
        return False, "virtual item (no library source)"
    if dto.get("LocationType") == "Virtual":
        return False, "virtual location"
    if dto.get("Type") in NON_EDITABLE_TYPES:
        return False, "container, not a media item"
    return True, None


def payload_matches_kind(payload: dict[str, Any], kind: ItemKind) -> bool:
    """The invariant asserted before every write (ADR 0003)."""
    return set(payload) == payload_field_set(kind)
