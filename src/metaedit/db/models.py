"""ORM models.

Two clearly separated concerns live in one database:

1. **Editor state** — ``snapshot`` and ``audit_log``: what we wrote to Jellyfin
   and what it looked like before, so any apply is reversible.
2. **The Last.fm archive** — a persistent, incremental, structured local copy of
   every response we ever received. Raw (``lastfm_request``/``lastfm_response``)
   is the source of truth; everything else is derived from it and rebuildable
   with no network access (see ``metaedit.archive.reindex``).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

HEX_DIGEST_LEN = 64


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Editor state
# ---------------------------------------------------------------------------


class Snapshot(Base):
    """An immutable pre-write copy of an item's writable fields."""

    __tablename__ = "snapshot"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    item_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str | None] = mapped_column(Text)
    fields: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    etag: Mapped[str | None] = mapped_column(Text)
    date_last_saved: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_op: Mapped[str] = mapped_column(String(16), nullable=False)
    batch_id: Mapped[str | None] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AuditLog(Base):
    """Provenance for every write attempt, successful or not."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    item_id: Mapped[str | None] = mapped_column(String(64), index=True)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    changed_fields: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    lastfm_candidate: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # Ties a written value back to the exact archived responses that justified it.
    lastfm_request_ids: Mapped[list[int]] = mapped_column(
        ARRAY(BigInteger), nullable=False, default=list
    )
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


# ---------------------------------------------------------------------------
# Archive: raw layer
# ---------------------------------------------------------------------------


class LastfmResponse(Base):
    """Content-addressed store of raw Last.fm response bodies.

    Identical bodies are stored once. ``observation_count``/``last_seen_at``
    record that we saw the same bytes again without paying to store them twice.
    """

    __tablename__ = "lastfm_response"

    # sha256 hex of the canonical (sorted-key, compact) JSON body.
    id: Mapped[str] = mapped_column(String(HEX_DIGEST_LEN), primary_key=True)
    body: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    body_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    is_error: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    observation_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class LastfmRequest(Base):
    """Append-only log of every Last.fm HTTP attempt (success or failure).

    Partitioned monthly by ``requested_at``. One row per attempt, so the log
    records *when* an entity appeared, changed, or vanished -- at a tiny
    fraction of the cost of duplicating response bodies.
    """

    __tablename__ = "lastfm_request"
    __table_args__ = (
        Index("idx_lastfm_request_params_hash", "params_hash"),
        Index("idx_lastfm_request_response_id", "response_id"),
        Index("idx_lastfm_request_method_time", "method", "requested_at"),
        {"postgresql_partition_by": "RANGE (requested_at)"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    method: Mapped[str] = mapped_column(String(64), nullable=False)
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    # sha256 hex of the canonicalised params (api_key excluded).
    params_hash: Mapped[str] = mapped_column(String(HEX_DIGEST_LEN), nullable=False)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    http_status: Mapped[int | None] = mapped_column(Integer)
    lastfm_error_code: Mapped[int | None] = mapped_column(Integer)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Reserved, and always false: this table logs HTTP attempts to Last.fm, and a
    # read served from the archive is the case where no attempt was made. Rows
    # with true are historical, from before reads stopped being logged here;
    # ``/api/archive/stats`` counts them as ``stray_archive_reads``.
    served_from_archive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    user_agent: Mapped[str | None] = mapped_column(Text)
    # Truncated sha256 of the API key: correlates requests to a key without storing it.
    api_key_fingerprint: Mapped[str | None] = mapped_column(String(16))
    response_id: Mapped[str | None] = mapped_column(
        String(HEX_DIGEST_LEN),
        ForeignKey("lastfm_response.id", ondelete="RESTRICT"),
        nullable=True,
    )


# ---------------------------------------------------------------------------
# Archive: derived layer (rebuildable from the raw layer alone)
# ---------------------------------------------------------------------------


class LastfmArtist(Base):
    __tablename__ = "lastfm_artist"
    __table_args__ = (
        UniqueConstraint("identity", name="uq_lastfm_artist_identity"),
        Index("idx_lastfm_artist_name_norm", "name_norm"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # coalesce(mbid, 'name:' || name_norm) -- the canonical identity key.
    identity: Mapped[str] = mapped_column(Text, nullable=False)
    mbid: Mapped[str | None] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    name_norm: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str | None] = mapped_column(Text)
    listeners: Mapped[int | None] = mapped_column(BigInteger)
    playcount: Mapped[int | None] = mapped_column(BigInteger)
    overview: Mapped[str | None] = mapped_column(Text)
    bio_published: Mapped[str | None] = mapped_column(Text)
    images: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    tags: Mapped[list[Any] | None] = mapped_column(JSONB)
    stats: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_request_id: Mapped[int | None] = mapped_column(BigInteger)
    latest_response_id: Mapped[str] = mapped_column(String(HEX_DIGEST_LEN), nullable=False)


class LastfmAlbum(Base):
    __tablename__ = "lastfm_album"
    __table_args__ = (
        UniqueConstraint("identity", name="uq_lastfm_album_identity"),
        Index("idx_lastfm_album_name_norm", "name_norm"),
        Index("idx_lastfm_album_artist_norm", "artist_name_norm"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    identity: Mapped[str] = mapped_column(Text, nullable=False)
    mbid: Mapped[str | None] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    name_norm: Mapped[str] = mapped_column(Text, nullable=False)
    artist_name: Mapped[str | None] = mapped_column(Text)
    artist_name_norm: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text)
    listeners: Mapped[int | None] = mapped_column(BigInteger)
    playcount: Mapped[int | None] = mapped_column(BigInteger)
    # `releasedate` is retained for tolerance: the current API no longer returns it
    # for album.getInfo, so it stays null, but a response that does carry it still
    # parses. See docs/design for the live-verified response shape.
    releasedate: Mapped[str | None] = mapped_column(Text)
    production_year: Mapped[int | None] = mapped_column(Integer)
    # Albums DO carry a wiki. Verified against the live API after the original code
    # asserted the opposite and discarded the text.
    overview: Mapped[str | None] = mapped_column(Text)
    wiki_published: Mapped[str | None] = mapped_column(Text)
    images: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    tags: Mapped[list[Any] | None] = mapped_column(JSONB)
    tracklist: Mapped[list[Any] | None] = mapped_column(JSONB)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_request_id: Mapped[int | None] = mapped_column(BigInteger)
    latest_response_id: Mapped[str] = mapped_column(String(HEX_DIGEST_LEN), nullable=False)


class LastfmTrack(Base):
    __tablename__ = "lastfm_track"
    __table_args__ = (
        UniqueConstraint("identity", name="uq_lastfm_track_identity"),
        Index("idx_lastfm_track_name_norm", "name_norm"),
        Index("idx_lastfm_track_artist_norm", "artist_name_norm"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    identity: Mapped[str] = mapped_column(Text, nullable=False)
    mbid: Mapped[str | None] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    name_norm: Mapped[str] = mapped_column(Text, nullable=False)
    artist_name: Mapped[str | None] = mapped_column(Text)
    artist_name_norm: Mapped[str | None] = mapped_column(Text)
    artist_mbid: Mapped[str | None] = mapped_column(String(64))
    album_name: Mapped[str | None] = mapped_column(Text)
    album_mbid: Mapped[str | None] = mapped_column(String(64))
    album_position: Mapped[int | None] = mapped_column(Integer)
    url: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int | None] = mapped_column(BigInteger)
    listeners: Mapped[int | None] = mapped_column(BigInteger)
    playcount: Mapped[int | None] = mapped_column(BigInteger)
    overview: Mapped[str | None] = mapped_column(Text)
    wiki_published: Mapped[str | None] = mapped_column(Text)
    tags: Mapped[list[Any] | None] = mapped_column(JSONB)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_request_id: Mapped[int | None] = mapped_column(BigInteger)
    latest_response_id: Mapped[str] = mapped_column(String(HEX_DIGEST_LEN), nullable=False)


class LastfmTagEdge(Base):
    """One row per (entity, tag, observation batch) -- the offline tag graph."""

    __tablename__ = "lastfm_tag_edge"
    __table_args__ = (
        UniqueConstraint("entity_kind", "entity_id", "tag_name_norm", name="uq_lastfm_tag_edge"),
        Index("idx_lastfm_tag_edge_tag", "tag_name_norm"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    entity_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    entity_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    tag_name: Mapped[str] = mapped_column(Text, nullable=False)
    tag_name_norm: Mapped[str] = mapped_column(Text, nullable=False)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    count: Mapped[int | None] = mapped_column(Integer)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    latest_response_id: Mapped[str] = mapped_column(String(HEX_DIGEST_LEN), nullable=False)


class LastfmSimilarity(Base):
    """Related artists. Stored here only -- never written to Jellyfin."""

    __tablename__ = "lastfm_similarity"
    __table_args__ = (
        UniqueConstraint("artist_id", "peer_name_norm", name="uq_lastfm_similarity"),
        Index("idx_lastfm_similarity_peer", "peer_name_norm"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    artist_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("lastfm_artist.id", ondelete="CASCADE"), nullable=False
    )
    peer_name: Mapped[str] = mapped_column(Text, nullable=False)
    peer_name_norm: Mapped[str] = mapped_column(Text, nullable=False)
    peer_mbid: Mapped[str | None] = mapped_column(String(64))
    peer_artist_id: Mapped[int | None] = mapped_column(BigInteger)
    match: Mapped[float | None] = mapped_column(Numeric(5, 4))
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    latest_response_id: Mapped[str] = mapped_column(String(HEX_DIGEST_LEN), nullable=False)


class LastfmArtistAlias(Base):
    """``autocorrect`` name corrections, so repeat lookups skip the network."""

    __tablename__ = "lastfm_artist_alias"
    __table_args__ = (UniqueConstraint("requested_name_norm", name="uq_lastfm_artist_alias"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    requested_name_norm: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_artist_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class LastfmEntityTag(Base):
    """Global tag index across the whole archive: which tags we have seen at all."""

    __tablename__ = "lastfm_entity_tag"
    __table_args__ = (UniqueConstraint("tag_name_norm", name="uq_lastfm_entity_tag"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tag_name: Mapped[str] = mapped_column(Text, nullable=False)
    tag_name_norm: Mapped[str] = mapped_column(Text, nullable=False)
    entity_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class ArchiveStat(Base):
    """Byte accounting against the Last.fm Reasonable Usage Cap."""

    __tablename__ = "archive_stat"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    measured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    payload_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    request_rows: Mapped[int] = mapped_column(BigInteger, nullable=False)
    response_rows: Mapped[int] = mapped_column(BigInteger, nullable=False)
    observations: Mapped[int] = mapped_column(BigInteger, nullable=False)
    oldest_request_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cap_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
