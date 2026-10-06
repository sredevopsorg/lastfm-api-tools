"""Typed models for the Last.fm read methods we use.

Every field is optional and every parser is forgiving. The documented XML samples
are the only contract Last.fm publishes, the JSON variant differs in small ways
(a single object where the XML implies a list, ``"count": ""`` instead of a
number, an empty string instead of a missing ``mbid``), and the archive must be
able to store whatever actually arrived. Being strict here would mean losing data
we already paid a rate-limited request for.

Field names are the wire names (lowercase) so the mapping to the API is obvious.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Last.fm returns error codes in the body; see https://www.last.fm/api/intro.
ERROR_INVALID_PARAMS = 6
ERROR_INVALID_RESOURCE = 7
ERROR_INVALID_API_KEY = 10
ERROR_TEMPORARILY_UNAVAILABLE = 16
ERROR_SUSPENDED_KEY = 26
ERROR_RATE_LIMIT_EXCEEDED = 29

RETRYABLE_ERROR_CODES = frozenset({ERROR_TEMPORARILY_UNAVAILABLE, ERROR_RATE_LIMIT_EXCEEDED})
FATAL_ERROR_CODES = frozenset({ERROR_INVALID_API_KEY, ERROR_SUSPENDED_KEY})


def _clean_str(value: Any) -> Any:
    """Last.fm sends ``""`` where it means "absent". Fold them together."""
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return value


def _to_list(value: Any) -> Any:
    """JSON gives a bare object where the XML implies a repeated element."""
    if value is None:
        return []
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return value
    return []


class LastfmImage(BaseModel):
    """A ``{size: url}`` map plus the ordered list of sizes seen."""

    model_config = ConfigDict(extra="allow")

    urls: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _from_wire(cls, value: Any) -> Any:
        """The wire format is ``[{"size": ..., "#text": ...}]``, not a map."""
        if isinstance(value, dict) and "urls" in value:
            return value
        entries = value if isinstance(value, list) else ([value] if value else [])
        urls: dict[str, str] = {}
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            size = entry.get("size")
            url = entry.get("#text")
            if size and url:
                urls[str(size)] = str(url)
        return {"urls": urls}

    def best(self) -> str | None:
        for size in ("mega", "extralarge", "large", "medium", "small"):
            if url := self.urls.get(size):
                return url
        return next(iter(self.urls.values()), None)


class LastfmTag(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    url: str | None = None
    count: int | None = None

    @field_validator("name", mode="before")
    @classmethod
    def _name(cls, value: Any) -> Any:
        # Defensive: some payloads nest the name/count pair under "name".
        if isinstance(value, dict):
            return _clean_str(value.get("name") or value.get("#text"))
        return _clean_str(value)

    @field_validator("count", mode="before")
    @classmethod
    def _count(cls, value: Any) -> Any:
        # Documented quirk: the JSON variant can send "" for a missing count.
        if value is None or value == "":
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None


class LastfmSimilarArtist(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    mbid: str | None = None
    match: float | None = None
    url: str | None = None
    image: LastfmImage = Field(default_factory=LastfmImage)

    @field_validator("name", "mbid", "url", mode="before")
    @classmethod
    def _clean(cls, value: Any) -> Any:
        return _clean_str(value)

    @field_validator("match", mode="before")
    @classmethod
    def _match(cls, value: Any) -> Any:
        if value is None or value == "":
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None


class LastfmStats(BaseModel):
    model_config = ConfigDict(extra="ignore")

    listeners: int | None = None
    plays: int | None = None

    @field_validator("listeners", "plays", mode="before")
    @classmethod
    def _int(cls, value: Any) -> Any:
        if value in (None, ""):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None


class LastfmWiki(BaseModel):
    """``bio`` on artists, ``wiki`` on tracks. Same shape either way."""

    model_config = ConfigDict(extra="ignore")

    published: str | None = None
    summary: str | None = None
    content: str | None = None

    @field_validator("published", "summary", "content", mode="before")
    @classmethod
    def _clean(cls, value: Any) -> Any:
        return _clean_str(value)


class LastfmArtistRef(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str | None = None
    mbid: str | None = None
    url: str | None = None

    @field_validator("name", "mbid", "url", mode="before")
    @classmethod
    def _clean(cls, value: Any) -> Any:
        return _clean_str(value)


class LastfmAlbumRef(BaseModel):
    model_config = ConfigDict(extra="ignore")

    artist: str | None = None
    title: str | None = None
    mbid: str | None = None
    url: str | None = None
    position: int | None = None
    image: LastfmImage = Field(default_factory=LastfmImage)

    @field_validator("artist", "title", "mbid", "url", mode="before")
    @classmethod
    def _clean(cls, value: Any) -> Any:
        return _clean_str(value)

    @field_validator("position", mode="before")
    @classmethod
    def _int(cls, value: Any) -> Any:
        if value in (None, ""):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None


class LastfmTrackInAlbum(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str | None = None
    mbid: str | None = None
    url: str | None = None
    duration: int | None = None
    rank: int | None = None
    artist: LastfmArtistRef | None = None
    streamable: Any = None

    @field_validator("name", "mbid", "url", mode="before")
    @classmethod
    def _clean(cls, value: Any) -> Any:
        return _clean_str(value)

    @field_validator("duration", "rank", mode="before")
    @classmethod
    def _int(cls, value: Any) -> Any:
        if value in (None, ""):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None


class LastfmAlbum(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str | None = None
    artist: str | None = None
    id: str | None = None
    mbid: str | None = None
    url: str | None = None
    releasedate: str | None = None
    listeners: int | None = None
    playcount: int | None = None
    image: LastfmImage = Field(default_factory=LastfmImage)
    # The current API returns the album tag list under `tags` (name and url only,
    # no counts) and leaves `toptags` absent. Both are read, so either shape works.
    toptags: list[LastfmTag] = Field(default_factory=list)
    tags: list[LastfmTag] = Field(default_factory=list)
    tracks: list[LastfmTrackInAlbum] = Field(default_factory=list)
    # Live-verified: album.getInfo does return a wiki with a usable summary.
    wiki: LastfmWiki | None = None

    @field_validator("name", "artist", "id", "mbid", "url", "releasedate", mode="before")
    @classmethod
    def _clean(cls, value: Any) -> Any:
        return _clean_str(value)

    @field_validator("listeners", "playcount", mode="before")
    @classmethod
    def _int(cls, value: Any) -> Any:
        if value in (None, ""):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @field_validator("toptags", "tags", mode="before")
    @classmethod
    def _tags(cls, value: Any) -> Any:
        return _tag_list(value)

    @field_validator("wiki", mode="before")
    @classmethod
    def _wiki(cls, value: Any) -> Any:
        return value if isinstance(value, dict) else None

    @field_validator("tracks", mode="before")
    @classmethod
    def _tracks(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return _to_list(value.get("track"))
        return _to_list(value)


class LastfmTrack(BaseModel):
    """``track.getInfo``. ``duration`` is milliseconds here, seconds in albums."""

    model_config = ConfigDict(extra="ignore")

    name: str | None = None
    id: str | None = None
    mbid: str | None = None
    url: str | None = None
    duration: int | None = None
    listeners: int | None = None
    playcount: int | None = None
    artist: LastfmArtistRef | None = None
    album: LastfmAlbumRef | None = None
    toptags: list[LastfmTag] = Field(default_factory=list)
    wiki: LastfmWiki | None = None

    @field_validator("name", "id", "mbid", "url", mode="before")
    @classmethod
    def _clean(cls, value: Any) -> Any:
        return _clean_str(value)

    @field_validator("duration", "listeners", "playcount", mode="before")
    @classmethod
    def _int(cls, value: Any) -> Any:
        if value in (None, ""):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @field_validator("toptags", mode="before")
    @classmethod
    def _tags(cls, value: Any) -> Any:
        return _tag_list(value)


class LastfmArtist(BaseModel):
    """``artist.getInfo``."""

    model_config = ConfigDict(extra="ignore")

    name: str | None = None
    mbid: str | None = None
    url: str | None = None
    streamable: Any = None
    image: LastfmImage = Field(default_factory=LastfmImage)
    stats: LastfmStats | None = None
    similar: list[LastfmSimilarArtist] = Field(default_factory=list)
    tags: list[LastfmTag] = Field(default_factory=list)
    bio: LastfmWiki | None = None
    # Present when autocorrect changed the requested name.
    corrected: str | None = None

    @field_validator("name", "mbid", "url", "corrected", mode="before")
    @classmethod
    def _clean(cls, value: Any) -> Any:
        return _clean_str(value)

    @field_validator("similar", mode="before")
    @classmethod
    def _similar(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return _to_list(value.get("artist"))
        return _to_list(value)

    @field_validator("tags", mode="before")
    @classmethod
    def _tags(cls, value: Any) -> Any:
        return _tag_list(value)


class LastfmTopTags(BaseModel):
    """``artist.getTopTags`` -- the only source of tag popularity counts."""

    model_config = ConfigDict(extra="ignore")

    artist: str | None = None
    tags: list[LastfmTag] = Field(default_factory=list)

    @field_validator("tags", mode="before")
    @classmethod
    def _tags(cls, value: Any) -> Any:
        # Wire shape is {"tag": [...]} on getTopTags, a bare list in getInfo.
        return _tag_list(value)


class LastfmSearchResult(BaseModel):
    """``artist.search`` / ``album.search`` -- candidate disambiguation."""

    model_config = ConfigDict(extra="ignore")

    name: str | None = None
    artist: str | None = None
    mbid: str | None = None
    url: str | None = None
    listeners: int | None = None
    image: LastfmImage = Field(default_factory=LastfmImage)

    @field_validator("name", "artist", "mbid", "url", mode="before")
    @classmethod
    def _clean(cls, value: Any) -> Any:
        return _clean_str(value)

    @field_validator("listeners", mode="before")
    @classmethod
    def _int(cls, value: Any) -> Any:
        if value in (None, ""):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None


def _tag_list(value: Any) -> list[dict[str, Any]]:
    """Normalise the several shapes a tag collection arrives in."""
    raw = value.get("tag") if isinstance(value, dict) else value
    return [entry for entry in _to_list(raw) if isinstance(entry, dict)]


def parse_artist(body: dict[str, Any]) -> LastfmArtist:
    return LastfmArtist.model_validate(body.get("artist") or {})


def parse_similar(body: dict[str, Any]) -> list[LastfmSimilarArtist]:
    container = body.get("similarartists") or {}
    return [
        LastfmSimilarArtist.model_validate(entry) for entry in _to_list(container.get("artist"))
    ]


def parse_top_tags(body: dict[str, Any]) -> LastfmTopTags:
    """``artist.getTopTags``. The wire key is the singular ``tag``."""
    container = body.get("toptags") or {}
    tag = container.get("tag")
    # getTopTags is the only method that returns popularity counts, so it is the
    # only one where ranking can be by count rather than by list order.
    return LastfmTopTags.model_validate({"artist": container.get("artist"), "tags": tag})


def parse_album(body: dict[str, Any]) -> LastfmAlbum:
    return LastfmAlbum.model_validate(body.get("album") or {})


def parse_track(body: dict[str, Any]) -> LastfmTrack:
    return LastfmTrack.model_validate(body.get("track") or {})


def parse_artist_search(body: dict[str, Any]) -> list[LastfmSearchResult]:
    container = body.get("results") or {}
    matches = container.get("artistmatches") or {}
    return [LastfmSearchResult.model_validate(entry) for entry in _to_list(matches.get("artist"))]


def parse_album_search(body: dict[str, Any]) -> list[LastfmSearchResult]:
    container = body.get("results") or {}
    matches = container.get("albummatches") or {}
    return [LastfmSearchResult.model_validate(entry) for entry in _to_list(matches.get("album"))]


def parse_error(body: dict[str, Any]) -> tuple[int, str]:
    code = int(body.get("error") or 0)
    message = str(body.get("message") or "Last.fm reported an error")
    return code, message
