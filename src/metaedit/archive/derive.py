"""Derive the archive's structured layer from its raw layer.

`docs/design/0003-derivation-and-reindex.md` is the contract; this module
implements §2-§6 of it. Two properties are load-bearing and both are testable:

**Purity.** Nothing here reads the clock or the network. Every derived timestamp
comes from a raw ``requested_at``, and ordering comes from explicit sorts over raw
columns. A ``now()`` in a derived column would make the store unreproducible while
looking perfectly healthy.

**Determinism of ids.** Derived primary keys are assigned by iterating entities in
``ORDER BY identity``, so rebuilding from the same raw rows reproduces the same
numbers. Any dict iteration order that leaked into a key would break that.

The derivation is expressed as pure functions over in-memory observations
(``derive_*``), with one thin database layer (``build_*``) that loads and writes.
That split is what makes the logic testable without Postgres.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from metaedit.adapters.lastfm.canonical import normalize_name, normalize_tag
from metaedit.archive.identity import (
    EntityKind,
    candidate_keys,
    key_for,
)

ENTITY_KINDS: tuple[EntityKind, ...] = ("artist", "album", "track")

# Methods we archive that legitimately carry no artist/album/track envelope. They
# are excluded from the "unexpected shape" count, so that count stays a usable
# signal: a drift in a getinfo response must not be masked by the presence of the
# methods we already know have another shape.
METHODS_WITHOUT_ENTITY_ENVELOPE = frozenset(
    {
        "artist.getsimilar",
        "artist.gettoptags",
        "artist.search",
        "album.search",
        "track.search",
    }
)


@dataclass(frozen=True, slots=True)
class RawObservation:
    """One archived response, with everything the derivation needs from the raw layer.

    Deliberately flat and free of ORM types: the derive functions must be usable
    on hand-built fixtures, which is how the reproducibility properties are tested.
    """

    request_id: int
    requested_at: datetime
    response_id: str
    body: dict[str, Any]
    method: str


@dataclass(slots=True)
class DerivedEntity:
    """A derived row, before it is given a primary key."""

    identity: str
    kind: EntityKind
    name: str | None
    name_norm: str
    mbid: str | None = None
    artist_name: str | None = None
    artist_name_norm: str | None = None
    artist_mbid: str | None = None
    album_name: str | None = None
    album_mbid: str | None = None
    album_position: int | None = None
    url: str | None = None
    listeners: int | None = None
    playcount: int | None = None
    duration_ms: int | None = None
    overview: str | None = None
    bio_published: str | None = None
    wiki_published: str | None = None
    releasedate: str | None = None
    production_year: int | None = None
    images: dict[str, Any] | None = None
    stats: dict[str, Any] | None = None
    tags: list[dict[str, Any]] = field(default_factory=list)
    tracklist: list[dict[str, Any]] = field(default_factory=list)
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None
    latest_response_id: str | None = None
    last_request_id: int | None = None

    def as_columns(self) -> dict[str, Any]:
        """Column values for insertion, excluding the assigned primary key."""
        data = {
            "identity": self.identity,
            "mbid": self.mbid,
            "name": self.name,
            "name_norm": self.name_norm,
            "url": self.url,
            "listeners": self.listeners,
            "playcount": self.playcount,
            "duration_ms": self.duration_ms,
            "overview": self.overview,
            "releasedate": self.releasedate,
            "production_year": self.production_year,
            "images": self.images,
            "tags": self.tags or None,
            "first_seen_at": self.first_seen_at,
            "last_seen_at": self.last_seen_at,
            "latest_response_id": self.latest_response_id,
            "last_request_id": self.last_request_id,
        }
        if self.kind == "artist":
            data["bio_published"] = self.bio_published
            data["stats"] = self.stats
        elif self.kind == "album":
            data["artist_name"] = self.artist_name
            data["artist_name_norm"] = self.artist_name_norm
            data["tracklist"] = self.tracklist or None
        else:
            data["artist_name"] = self.artist_name
            data["artist_name_norm"] = self.artist_name_norm
            data["artist_mbid"] = self.artist_mbid
            data["album_name"] = self.album_name
            data["album_mbid"] = self.album_mbid
            data["album_position"] = self.album_position
            data["wiki_published"] = self.wiki_published
        return data


@dataclass(frozen=True, slots=True)
class DerivedTagEdge:
    """One (entity, tag) pair.

    Carries the entity *identity* rather than a numeric id: primary keys are
    assigned later, when entities are inserted in identity order, so the
    derivation must not depend on them.
    """

    entity_kind: str
    entity_identity: str
    tag_name: str
    tag_name_norm: str
    rank: int
    count: int | None
    observed_at: datetime
    latest_response_id: str


@dataclass(frozen=True, slots=True)
class DerivedSimilarity:
    artist_identity: str
    peer_name: str
    peer_name_norm: str
    peer_mbid: str | None
    match: float | None
    rank: int
    observed_at: datetime
    latest_response_id: str


@dataclass(frozen=True, slots=True)
class DerivedAlias:
    requested_name_norm: str
    canonical_identity: str
    observed_at: datetime


@dataclass(slots=True)
class DerivationResult:
    artists: list[DerivedEntity] = field(default_factory=list)
    albums: list[DerivedEntity] = field(default_factory=list)
    tracks: list[DerivedEntity] = field(default_factory=list)
    tag_edges: list[DerivedTagEdge] = field(default_factory=list)
    similarities: list[DerivedSimilarity] = field(default_factory=list)
    aliases: list[DerivedAlias] = field(default_factory=list)

    # Observations that produced no entity because their shape was not recognised
    # at all. This exists because the failure mode of a parsing mismatch is
    # *absence*: an unrecognised payload yields fewer entities and nothing else
    # says so. A count that grows is the only signal that Last.fm's response shape
    # has drifted, or that a method we do not model is being archived.
    #
    # Note this counts by *response*, not per entity kind, and it legitimately
    # includes methods outside the structural derivation (artist.getsimilar,
    # *.search), which carry no artist/album/track envelope by design.
    skipped_unrecognised_shape: int = 0
    skipped_expected_no_envelope: int = 0

    @property
    def observations_with_entities(self) -> int:
        return len(self.artists) + len(self.albums) + len(self.tracks)

    @property
    def unexpected_shapes(self) -> int:
        """Bodies we could not use that we also did not expect to be envelope-free.

        This is the number worth alerting on: zero means every archived body was
        either understood or is one of the methods known to carry another shape.
        """
        return self.skipped_unrecognised_shape

    def entities(self) -> list[DerivedEntity]:
        return [*self.artists, *self.albums, *self.tracks]


# --------------------------------------------------------------------------- #
# Payload extraction
# --------------------------------------------------------------------------- #


def _as_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _clean(value: Any) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    return None


def _images(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, list):
        return None
    urls = {
        str(entry["size"]): str(entry["#text"])
        for entry in value
        if isinstance(entry, dict) and entry.get("size") and entry.get("#text")
    }
    return urls or None


def _tag_entries(value: Any) -> list[dict[str, Any]]:
    """Normalise the several shapes a tag collection arrives in.

    Returns ``{name, count, url}`` dicts in source order. ``count`` is only ever
    present when Last.fm supplied it (``artist.getTopTags``); it is never
    fabricated, because a made-up popularity would be indistinguishable from real
    data downstream.
    """
    container = value if isinstance(value, dict) else {}
    if isinstance(value, list):
        raw: Any = value
    else:
        raw = container.get("tag")
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    entries: list[dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        name = _clean(entry.get("name"))
        if not name:
            continue
        entries.append(
            {
                "name": name,
                "count": _as_int(entry.get("count")),
                "url": _clean(entry.get("url")),
            }
        )
    return entries


def _parse_releasedate(value: Any) -> int | None:
    """``"6 Apr 1999, 00:00"`` -> 1999. Last.fm's album release format."""
    text = _clean(value)
    if not text:
        return None
    for token in text.replace(",", " ").split():
        if len(token) == 4 and token.isdigit():
            year = int(token)
            if 1000 <= year <= 2999:
                return year
    return None


def _artist_body(body: dict[str, Any]) -> dict[str, Any] | None:
    artist = body.get("artist")
    return artist if isinstance(artist, dict) else None


def _album_body(body: dict[str, Any]) -> dict[str, Any] | None:
    album = body.get("album")
    return album if isinstance(album, dict) else None


def _track_body(body: dict[str, Any]) -> dict[str, Any] | None:
    track = body.get("track")
    return track if isinstance(track, dict) else None


# --------------------------------------------------------------------------- #
# Grouping
# --------------------------------------------------------------------------- #


def _entity_identity_for(kind: EntityKind, payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Identity plus the name/artist components used to build it."""
    mbid = _clean(payload.get("mbid"))
    if kind == "artist":
        name = _clean(payload.get("name"))
        return key_for(kind, mbid=mbid, name=name), {"mbid": mbid, "name": name, "artist": None}
    if kind == "album":
        name = _clean(payload.get("name"))
        artist = _clean(payload.get("artist"))
        return key_for(kind, mbid=mbid, artist=artist, name=name), {
            "mbid": mbid,
            "name": name,
            "artist": artist,
        }
    name = _clean(payload.get("name"))
    artist_ref = payload.get("artist")
    artist = _clean(artist_ref.get("name")) if isinstance(artist_ref, dict) else None
    return key_for(kind, mbid=mbid, artist=artist, name=name), {
        "mbid": mbid,
        "name": name,
        "artist": artist,
    }


GroupedItem = tuple[RawObservation, dict[str, Any], str, dict[str, Any]]


def _group_observations(
    kind: EntityKind, observations: Iterable[RawObservation]
) -> dict[str, list[GroupedItem]]:
    """Group observations by resolved identity, absorbing name keys into MBID keys.

    See the design document §2. The result is keyed by the surviving identity; the
    input order does not matter, which is what makes the outcome reproducible even
    when an entity gains an MBID partway through the archive's history.
    """
    parsed: list[GroupedItem] = []
    for observation in observations:
        payload = _payload_for(kind, observation.body)
        if payload is None:
            continue
        identity, components = _entity_identity_for(kind, payload)
        if not identity:
            # No name and no MBID: nothing to key on. Counted by the caller as an
            # unrecognised response, which is the signal that matters.
            continue
        parsed.append((observation, payload, identity, components))

    # Group everything by the MBID when there is one, else by the name key.
    groups: dict[str, list[GroupedItem]] = {}
    for item in parsed:
        groups.setdefault(item[2], []).append(item)

    # Fold name-keyed groups into the MBID-keyed group that supersedes them.
    resolved: dict[str, list[GroupedItem]] = {}
    absorbing: dict[str, str] = {}  # name key -> mbid key
    for identity, items in groups.items():
        components = items[0][3]
        for candidate in candidate_keys(
            kind,
            mbid=components["mbid"],
            artist=components["artist"],
            name=components["name"],
        ):
            if candidate != identity:
                absorbing[candidate] = identity

    for identity, items in groups.items():
        target = identity
        # Only a name-keyed group is absorbed; an MBID key never merges into a name.
        while target in absorbing and absorbing[target] != target:
            target = absorbing[target]
        resolved.setdefault(target, []).extend(items)

    return resolved


def _payload_for(kind: EntityKind, body: dict[str, Any]) -> dict[str, Any] | None:
    if kind == "artist":
        return _artist_body(body)
    if kind == "album":
        return _album_body(body)
    return _track_body(body)


# --------------------------------------------------------------------------- #
# Field selection
# --------------------------------------------------------------------------- #


def _select_fields(kind: EntityKind, identity: str, items: Sequence[Any]) -> DerivedEntity:
    """Build one entity row from all of its observations.

    Ordering is ``(requested_at, request_id)`` so two observations in the same
    second still resolve deterministically. The newest *non-error* observation
    supplies the parsed fields, and a ``None`` there genuinely overwrites: Last.fm
    really does remove data, and the raw layer keeps the previous value.
    """
    ordered = sorted(items, key=lambda item: (item[0].requested_at, item[0].request_id))
    first = ordered[0][0].requested_at
    last = ordered[-1][0].requested_at

    newest: Any | None = None
    for item in reversed(ordered):
        payload = item[1]
        if _clean(payload.get("name")) or _clean(payload.get("mbid")):
            newest = item
            break
    if newest is None:
        newest = ordered[-1]

    observation, payload, _, components = newest
    entity = DerivedEntity(
        identity=identity,
        kind=kind,
        name=_clean(payload.get("name")),
        name_norm=normalize_name(_clean(payload.get("name"))),
        mbid=components["mbid"],
        first_seen_at=first,
        last_seen_at=last,
        latest_response_id=observation.response_id,
        last_request_id=observation.request_id,
        url=_clean(payload.get("url")),
        listeners=_as_int(payload.get("listeners")),
        playcount=_as_int(payload.get("playcount")),
        images=_images(payload.get("image")),
        tags=_tag_entries(payload.get("toptags") or payload.get("tags")),
    )

    if kind == "artist":
        stats = payload.get("stats")
        entity.stats = dict(stats) if isinstance(stats, dict) else None
        bio = payload.get("bio")
        if isinstance(bio, dict):
            entity.overview = _clean(bio.get("summary"))
            entity.bio_published = _clean(bio.get("published"))
    elif kind == "album":
        entity.artist_name = _clean(payload.get("artist"))
        entity.artist_name_norm = normalize_name(entity.artist_name)
        entity.releasedate = _clean(payload.get("releasedate"))
        entity.production_year = _parse_releasedate(entity.releasedate)
        entity.tracklist = _tracklist(payload.get("tracks"))
        # Album getInfo carries no wiki: leaving overview unset is honest.
    else:
        artist_ref = payload.get("artist")
        if isinstance(artist_ref, dict):
            entity.artist_name = _clean(artist_ref.get("name"))
            entity.artist_mbid = _clean(artist_ref.get("mbid"))
        entity.artist_name_norm = normalize_name(entity.artist_name)
        album_ref = payload.get("album")
        if isinstance(album_ref, dict):
            entity.album_name = _clean(album_ref.get("title"))
            entity.album_mbid = _clean(album_ref.get("mbid"))
            entity.album_position = _as_int(album_ref.get("position"))
            if entity.images is None:
                entity.images = _images(album_ref.get("image"))
        wiki = payload.get("wiki")
        if isinstance(wiki, dict):
            entity.overview = _clean(wiki.get("summary"))
            entity.wiki_published = _clean(wiki.get("published"))
        # track.getInfo duration is milliseconds.
        entity.duration_ms = _as_int(payload.get("duration"))

    return entity


def _tracklist(value: Any) -> list[dict[str, Any]]:
    container = value if isinstance(value, dict) else {}
    raw: Any = value if isinstance(value, list) else container.get("track")
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    entries: list[dict[str, Any]] = []
    for rank, entry in enumerate(raw):
        if not isinstance(entry, dict):
            continue
        name = _clean(entry.get("name"))
        if not name:
            continue
        artist_ref = entry.get("artist")
        declared_rank = _as_int(entry.get("rank"))
        # Album track durations are seconds; normalise to ms like track.getInfo.
        seconds = _as_int(entry.get("duration"))
        entries.append(
            {
                "name": name,
                "rank": declared_rank if declared_rank is not None else rank,
                "duration_ms": seconds * 1000 if seconds is not None else None,
                "mbid": _clean(entry.get("mbid")),
                "artist": _clean(artist_ref.get("name")) if isinstance(artist_ref, dict) else None,
            }
        )
    return entries


# --------------------------------------------------------------------------- #
# Graph derivation
# --------------------------------------------------------------------------- #


def derive_tag_edges(kind: EntityKind, entities: Sequence[DerivedEntity]) -> list[DerivedTagEdge]:
    """One row per (entity, tag), replacing the previous set for that entity.

    ``rank`` is the source position and ``count`` is preserved only where Last.fm
    provided it, so the tag policy in phase 4 can rank by real popularity where it
    exists and fall back to list order where it does not.
    """
    edges: list[DerivedTagEdge] = []
    for entity in entities:
        seen: set[str] = set()
        for rank, tag in enumerate(entity.tags):
            norm = normalize_tag(tag.get("name"))
            if not norm or norm in seen:
                continue
            seen.add(norm)
            edges.append(
                DerivedTagEdge(
                    entity_kind=kind,
                    entity_identity=entity.identity,
                    tag_name=str(tag.get("name")),
                    tag_name_norm=norm,
                    rank=rank,
                    count=tag.get("count"),
                    observed_at=entity.last_seen_at or entity.first_seen_at,  # type: ignore[arg-type]
                    latest_response_id=entity.latest_response_id or "",
                )
            )
    return edges


def derive_similarities(
    artists: Sequence[DerivedEntity],
    observations: Sequence[RawObservation],
    *,
    owners: Mapping[int, str] | None = None,
) -> list[DerivedSimilarity]:
    """Similar artists, taken from ``artist.getsimilar`` observations.

    Determining the owning artist is fiddlier than it looks. The API call is
    ``artist.getsimilar&artist=Cher``, and the response carries ``Cher`` as an
    *attribute* on the container while the peers are repeated ``<artist>``
    *children*. Flattened to JSON both land on the key ``"artist"``, and converters
    disagree on which wins -- one yields the name, another yields the list. So the
    owner is taken from ``owners[request_id]`` when the caller can supply it (the
    request params are the authoritative record of what was asked), and otherwise
    read from the container attribute only if it is a string.

    Only artists we already know about are linked, and the peer is never invented:
    a similarity pointing at an entity we have never fetched is still recorded,
    with ``peer_mbid`` from the payload, because the reference is the data.
    """
    owners = owners or {}
    by_name: dict[str, str] = {}
    for artist in artists:
        if artist.name_norm:
            by_name.setdefault(artist.name_norm, artist.identity)

    result: list[DerivedSimilarity] = []
    for observation in sorted(observations, key=lambda o: (o.requested_at, o.request_id)):
        container = observation.body.get("similarartists")
        if not isinstance(container, dict):
            continue
        owner = owners.get(observation.request_id) or _clean_string_attr(container.get("artist"))
        owner_identity = by_name.get(normalize_name(owner))
        if owner_identity is None:
            continue
        peers = container.get("artist")
        if isinstance(peers, str):
            # The converter kept the attribute and dropped the children: we have
            # the owner but no peers, which is not an error, just no data.
            continue
        if isinstance(peers, dict):
            peers = [peers]
        if not isinstance(peers, list):
            continue
        for rank, peer in enumerate(peers):
            if not isinstance(peer, dict):
                continue
            peer_name = _clean(peer.get("name"))
            if not peer_name:
                continue
            result.append(
                DerivedSimilarity(
                    artist_identity=owner_identity,
                    peer_name=peer_name,
                    peer_name_norm=normalize_name(peer_name),
                    peer_mbid=_clean(peer.get("mbid")),
                    match=_as_float(peer.get("match")),
                    rank=rank,
                    observed_at=observation.requested_at,
                    latest_response_id=observation.response_id,
                )
            )
    return _dedupe_similarities(result)


def _dedupe_similarities(rows: Sequence[DerivedSimilarity]) -> list[DerivedSimilarity]:
    """Keep the newest row per (artist, peer), then order deterministically."""
    newest: dict[tuple[str, str], DerivedSimilarity] = {}
    for row in rows:
        key = (row.artist_identity, row.peer_name_norm)
        existing = newest.get(key)
        if existing is None or (row.observed_at, row.rank) >= (existing.observed_at, existing.rank):
            newest[key] = row
    return sorted(
        newest.values(), key=lambda row: (row.artist_identity, row.rank, row.peer_name_norm)
    )


def derive_aliases(corrections: Sequence[tuple[str, str, datetime]]) -> list[DerivedAlias]:
    """``autocorrect`` corrections: requested name -> canonical identity.

    Input is ``(requested_artist, canonical_identity, observed_at)`` triples. The
    requested spelling only exists in a request's *params*, and the canonical
    spelling only in the response *body*, so this is a join across the two raw
    tables rather than something derivable from a response alone. The database
    layer performs that join and passes the pairs in, which keeps this function
    pure and testable.

    Last.fm's autocorrect is effectively permanent, so the newest observation wins
    for a given requested spelling.
    """
    newest: dict[str, DerivedAlias] = {}
    for requested, canonical_identity, observed_at in sorted(
        corrections, key=lambda item: (item[2], item[1])
    ):
        norm = normalize_name(requested)
        if not norm or not canonical_identity:
            continue
        newest[norm] = DerivedAlias(
            requested_name_norm=norm,
            canonical_identity=canonical_identity,
            observed_at=observed_at,
        )
    return [newest[norm] for norm in sorted(newest)]


def derive_entity_tag_counts(edges: Sequence[DerivedTagEdge]) -> list[tuple[str, str, int]]:
    """The archive-wide tag aggregate: ``(tag_name, tag_name_norm, entity_count)``.

    Counts *distinct entities* carrying a tag, so the same artist observed many
    times with the same tag counts once. Keyed by identity, which is stable across
    runs, rather than by a primary key that is assigned during insertion.
    """
    tags: dict[str, str] = {}
    carriers: dict[str, set[tuple[str, str]]] = {}
    for edge in edges:
        tags.setdefault(edge.tag_name_norm, edge.tag_name)
        carriers.setdefault(edge.tag_name_norm, set()).add((edge.entity_kind, edge.entity_identity))
    return [(tags[norm], norm, len(carriers[norm])) for norm in sorted(tags)]


# --------------------------------------------------------------------------- #
# Top-level derivation
# --------------------------------------------------------------------------- #


def _clean_string_attr(value: Any) -> str | None:
    """The container attribute, only when it really is a string."""
    return _clean(value) if isinstance(value, str) else None


def derive_all(
    observations: Sequence[RawObservation],
    *,
    alias_corrections: Sequence[tuple[str, str, datetime]] = (),
    similarity_owners: Mapping[int, str] | None = None,
) -> DerivationResult:
    """Derive every table from a set of raw observations. Pure and deterministic."""
    artists = _derive_entities("artist", observations)
    albums = _derive_entities("album", observations)
    tracks = _derive_entities("track", observations)

    tag_edges = [
        *derive_tag_edges("artist", artists),
        *derive_tag_edges("album", albums),
        *derive_tag_edges("track", tracks),
    ]

    # Counted by response id and by what the derivation *actually produced*, not by
    # whether an envelope happens to be present: a body can carry an {"artist": ...}
    # envelope and still be unusable (no name and no MBID), and that is precisely the
    # silent-loss case this counter exists for. Responses that yielded no entity
    # anywhere, and whose method was not expected to be envelope-free, are the signal.
    produced = {entity.latest_response_id for entity in (*artists, *albums, *tracks)}
    without_expected_envelope = {
        observation.response_id
        for observation in observations
        if observation.method in METHODS_WITHOUT_ENTITY_ENVELOPE
    }
    all_responses = {observation.response_id for observation in observations}

    return DerivationResult(
        artists=artists,
        albums=albums,
        tracks=tracks,
        tag_edges=tag_edges,
        similarities=derive_similarities(artists, observations, owners=similarity_owners),
        aliases=derive_aliases(alias_corrections),
        # Bodies that produced nothing, split by whether the method was expected to
        # be envelope-free.
        skipped_unrecognised_shape=len(all_responses - produced - without_expected_envelope),
        skipped_expected_no_envelope=len(without_expected_envelope),
    )


def _derive_entities(
    kind: EntityKind, observations: Sequence[RawObservation]
) -> list[DerivedEntity]:
    """Entities for one kind, ordered by identity so ids are reproducible."""
    relevant = [o for o in observations if _payload_for(kind, o.body) is not None]
    groups = _group_observations(kind, relevant)
    return [_select_fields(kind, identity, groups[identity]) for identity in sorted(groups)]
