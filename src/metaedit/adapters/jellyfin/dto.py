"""Jellyfin DTOs.

Only the fields this application reads and writes are modelled. The whole
``BaseItemDto`` has ~155 properties, most of them server-computed; round-tripping
them would push derived values back at the server and widen the blast radius of
a mistake (ADR 0003).

Names are the wire names (PascalCase) -- no aliasing cleverness, so what the code
says is what goes on the wire.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

ItemKind = Literal["MusicArtist", "MusicAlbum", "Audio"]

KIND_LABEL: dict[ItemKind, str] = {
    "MusicArtist": "artist",
    "MusicAlbum": "album",
    "Audio": "song",
}


class NameGuidPair(BaseModel):
    model_config = ConfigDict(extra="ignore")

    Name: str | None = None
    Id: str | None = None


class ExternalUrl(BaseModel):
    model_config = ConfigDict(extra="ignore")

    Name: str | None = None
    Url: str | None = None


class BaseItemDto(BaseModel):
    """The subset of ``BaseItemDto`` this application understands.

    Unmodelled properties are ignored rather than rejected: Jellyfin adds fields
    between releases and a strict model would make the app brittle for no gain.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    # --- identity / display (read-only) ---
    Id: str | None = None
    ServerId: str | None = None
    Etag: str | None = None
    Type: str | None = None
    MediaType: str | None = None
    SourceType: str | None = None
    LocationType: str | None = None
    IsFolder: bool | None = None
    Path: str | None = None
    DateCreated: datetime | None = None
    DateLastSaved: datetime | None = None
    DateLastRefreshed: datetime | None = None
    ChildCount: int | None = None
    RecursiveItemCount: int | None = None
    RunTimeTicks: int | None = None

    # --- writable (the whitelist mirrors this set) ---
    Name: str | None = None
    ForcedSortName: str | None = None
    OriginalTitle: str | None = None
    Overview: str | None = None
    Genres: list[str] | None = None
    Tags: list[str] | None = None
    Studios: list[NameGuidPair] | None = None
    ProductionLocations: list[str] | None = None
    ProviderIds: dict[str, str] | None = None
    ExternalUrls: list[ExternalUrl] | None = None
    CommunityRating: float | None = None
    CriticRating: float | None = None
    PremiereDate: datetime | None = None
    ProductionYear: int | None = None
    OfficialRating: str | None = None
    CustomRating: str | None = None
    PreferredMetadataLanguage: str | None = None
    PreferredMetadataCountryCode: str | None = None
    People: list[dict[str, Any]] | None = None
    LockData: bool | None = None
    LockedFields: list[str] | None = None

    # Present for artists; omitted by the server for albums and songs.
    ArtistItems: list[NameGuidPair] | None = None
    # Needed for songs, and for detecting album-artist drift.
    AlbumArtists: list[NameGuidPair] | None = None
    # The scalar form of the album artist, which the server often sends for albums
    # *instead of* `AlbumArtists`. Both are in the vendored contract, and modelling only
    # the array meant an album could yield no artist at all -- which silently made the
    # Last.fm lookup for an album impossible to derive.
    AlbumArtist: str | None = None
    # A plain list of names, the third spelling the contract allows.
    Artists: list[str] | None = None

    # --- music-specific display helpers ---
    Album: str | None = None
    AlbumId: str | None = None

    def names(self) -> list[str]:
        """The item's own credited artists, from whichever spelling the server sent."""
        from_items = [pair.Name for pair in (self.ArtistItems or []) if pair.Name]
        return from_items or [name for name in (self.Artists or []) if name]

    def album_artist_names(self) -> list[str]:
        """The album artist, from whichever spelling the server sent.

        Ordered by specificity: the structured array, then the plain list, then the
        scalar. Returning an empty list here is not harmless -- an album with no artist
        cannot be looked up on Last.fm at all.
        """
        from_pairs = [pair.Name for pair in (self.AlbumArtists or []) if pair.Name]
        if from_pairs:
            return from_pairs
        if self.AlbumArtist:
            return [self.AlbumArtist]
        return [name for name in (self.Artists or []) if name]


class BaseItemDtoQueryResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    Items: list[BaseItemDto] = Field(default_factory=list)
    TotalRecordCount: int = 0
    StartIndex: int | None = None


class VirtualFolderInfo(BaseModel):
    model_config = ConfigDict(extra="ignore")

    Name: str | None = None
    ItemId: str | None = None
    Locations: list[str] | None = None
    CollectionType: str | None = None
    LibraryOptions: dict[str, Any] | None = None


class UserPolicy(BaseModel):
    model_config = ConfigDict(extra="ignore")

    IsAdministrator: bool | None = None
    EnableCollectionManagement: bool | None = None
    EnableMetadataManagement: bool | None = None


class UserDto(BaseModel):
    model_config = ConfigDict(extra="ignore")

    Id: str | None = None
    Name: str | None = None
    Policy: UserPolicy = Field(default_factory=UserPolicy)


class SystemInfoPublic(BaseModel):
    model_config = ConfigDict(extra="ignore")

    ServerName: str | None = None
    Version: str | None = None
    Id: str | None = None


class MetadataEditorInfo(BaseModel):
    """``GET /Items/{itemId}/MetadataEditor`` -- provider keys and content types."""

    model_config = ConfigDict(extra="ignore")

    ExternalIdInfos: list[dict[str, Any]] = Field(default_factory=list)
    ContentType: str | None = None
    ContentTypeOptions: list[dict[str, Any]] = Field(default_factory=list)
