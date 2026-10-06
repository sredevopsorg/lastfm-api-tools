"""Rebuild the derived archive tables from the raw archive.

Implements `docs/design/0003-derivation-and-reindex.md` §7-§8. The raw layer
(``lastfm_request`` + ``lastfm_response``) is the source of truth; everything here
is a pure function of it, so changing a parsing model or the tag split is a reindex
rather than a re-crawl.

Two rules shape the implementation:

* **No network, no clock.** Nothing in this module may construct a ``LastfmClient``
  or read ``now()``. Derived timestamps come from raw ``requested_at`` values.
* **The raw layer is never touched.** ``lastfm_request`` and ``lastfm_response``
  are append-only; this module only reads them.

The swap is done by TRUNCATE + INSERT ... SELECT inside one transaction rather than
by renaming tables. TRUNCATE inside a transaction is transactional in Postgres, and
INSERT ... SELECT preserves the explicit primary keys we assign, so the derived ids
are identical after a rebuild. A rename-based swap would be equally atomic but would
require carrying the ids through, for no additional benefit here.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, insert, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from metaedit.adapters.lastfm.canonical import canonical_params, normalize_name
from metaedit.archive.derive import (
    METHODS_WITHOUT_ENTITY_ENVELOPE,
    DerivationResult,
    DerivedEntity,
    RawObservation,
    derive_all,
    derive_entity_tag_counts,
)
from metaedit.config import Settings
from metaedit.db.models import (
    LastfmAlbum,
    LastfmArtist,
    LastfmArtistAlias,
    LastfmEntityTag,
    LastfmRequest,
    LastfmResponse,
    LastfmSimilarity,
    LastfmTagEdge,
    LastfmTrack,
)
from metaedit.db.session import get_session_factory, init_engine
from metaedit.logging import get_logger

log = get_logger(__name__)

DERIVED_TABLES = (
    "lastfm_artist",
    "lastfm_album",
    "lastfm_track",
    "lastfm_tag_edge",
    "lastfm_similarity",
    "lastfm_artist_alias",
    "lastfm_entity_tag",
)

ENTITY_MODELS = {
    "artist": LastfmArtist,
    "album": LastfmAlbum,
    "track": LastfmTrack,
}


class ReindexError(RuntimeError):
    """A derivation cannot be applied. Nothing has been written when this is raised."""


@dataclass(slots=True)
class ReindexReport:
    dry_run: bool
    artists: int = 0
    albums: int = 0
    tracks: int = 0
    tag_edges: int = 0
    similarities: int = 0
    aliases: int = 0
    entity_tags: int = 0
    observations: int = 0
    response_bodies: int = 0
    # Archived bodies that carried no entity envelope and were not expected to be
    # envelope-free. A number that grows means the derivation is silently discarding
    # data, which is otherwise invisible; zero is the healthy value.
    unexpected_shapes: int = 0
    # Bodies from methods known to carry another shape (artist.getsimilar, *.search).
    expected_no_envelope: int = 0
    duration_ms: int = 0
    changes: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "counts": {
                "artists": self.artists,
                "albums": self.albums,
                "tracks": self.tracks,
                "tag_edges": self.tag_edges,
                "similarities": self.similarities,
                "aliases": self.aliases,
                "entity_tags": self.entity_tags,
                "observations": self.observations,
                "response_bodies": self.response_bodies,
                "unexpected_shapes": self.unexpected_shapes,
                "expected_no_envelope": self.expected_no_envelope,
            },
            "duration_ms": self.duration_ms,
            "changes": self.changes,
        }


# --------------------------------------------------------------------------- #
# Loading the raw layer
# --------------------------------------------------------------------------- #


async def load_observations(
    session: AsyncSession, *, since: datetime | None = None
) -> list[RawObservation]:
    """Read the raw layer as observations, ordered deterministically.

    The order is ``(requested_at, request_id)`` so a caller sees the same sequence
    on every run; the derivation re-sorts where it matters, but stable input makes
    failures reproducible.
    """
    stmt = (
        select(
            LastfmRequest.id,
            LastfmRequest.requested_at,
            LastfmResponse.id,
            LastfmResponse.body,
            LastfmRequest.method,
        )
        .join(LastfmResponse, LastfmResponse.id == LastfmRequest.response_id)
        .where(LastfmResponse.is_error.is_(False))
        .order_by(LastfmRequest.requested_at, LastfmRequest.id)
    )
    if since is not None:
        stmt = stmt.where(LastfmRequest.requested_at >= since)

    rows = (await session.execute(stmt)).all()
    return [
        RawObservation(
            request_id=int(row[0]),
            requested_at=row[1],
            response_id=row[2],
            body=row[3],
            method=row[4],
        )
        for row in rows
    ]


async def load_similarity_owners(session: AsyncSession) -> dict[int, str]:
    """Map request id -> the artist name that was asked for.

    The owning artist of an ``artist.getsimilar`` response only exists in the
    request params (``docs/design/0003`` §6), so it is loaded separately and passed
    into the derivation rather than guessed from the body.
    """
    stmt = (
        select(LastfmRequest.id, LastfmRequest.params)
        .where(LastfmRequest.method == "artist.getsimilar")
        .order_by(LastfmRequest.requested_at, LastfmRequest.id)
    )
    owners: dict[int, str] = {}
    for request_id, params in (await session.execute(stmt)).all():
        canonical = canonical_params("artist.getsimilar", params or {})
        artist = canonical.get("artist")
        if isinstance(artist, str) and artist:
            owners[int(request_id)] = artist
    return owners


async def load_alias_corrections(
    session: AsyncSession,
) -> list[tuple[str, str, datetime]]:
    """``(requested_artist, canonical_identity, observed_at)`` for real corrections.

    A join across the two raw tables: the requested spelling is only in the params
    and the canonical spelling only in the body. Rows where the two agree are not
    corrections and are excluded, so the alias table stays a list of genuine
    misspellings rather than a copy of every request.
    """
    from metaedit.archive.identity import artist_identity

    stmt = (
        select(LastfmRequest.params, LastfmResponse.body, LastfmRequest.requested_at)
        .join(LastfmResponse, LastfmResponse.id == LastfmRequest.response_id)
        .where(LastfmRequest.method == "artist.getinfo")
        .where(LastfmResponse.is_error.is_(False))
        .order_by(LastfmRequest.requested_at, LastfmRequest.id)
    )
    corrections: dict[str, tuple[str, str, datetime]] = {}
    for params, body, requested_at in (await session.execute(stmt)).all():
        canonical = canonical_params("artist.getinfo", params or {})
        requested = canonical.get("artist")
        if not isinstance(requested, str) or not requested.strip():
            continue
        artist = (body or {}).get("artist")
        if not isinstance(artist, dict):
            continue
        canonical_name = artist.get("name")
        if not isinstance(canonical_name, str) or not canonical_name.strip():
            continue
        if normalize_name(requested) == normalize_name(canonical_name):
            continue
        identity = artist_identity(mbid=artist.get("mbid"), name=canonical_name)
        if not identity:
            continue
        corrections[normalize_name(requested)] = (requested, identity, requested_at)
    return [corrections[key] for key in sorted(corrections)]


# --------------------------------------------------------------------------- #
# Assigning primary keys
# --------------------------------------------------------------------------- #


def assign_ids(result: DerivationResult) -> dict[str, dict[str, int]]:
    """Give every derived row a deterministic primary key, keyed by table.

    Ids are assigned by enumerating rows in a fixed order, never by a sequence.
    That is what makes a rebuild reproduce the same numbers: if the sequence chose
    them, the second rebuild would number the same rows differently and the derived
    layer would only be *logically* reproducible rather than actually identical.

    Keys are the natural identity of each row, which is also its uniqueness
    constraint, so the ordering can never be ambiguous.
    """
    ids: dict[str, dict[str, int]] = {}

    entity_keys: dict[str, dict[str, int]] = {"artist": {}, "album": {}, "track": {}}
    for kind, entities in (
        ("artist", result.artists),
        ("album", result.albums),
        ("track", result.tracks),
    ):
        for position, entity in enumerate(entities, start=1):
            entity_keys[kind][entity.identity] = position
    ids["lastfm_artist"] = entity_keys["artist"]
    ids["lastfm_album"] = entity_keys["album"]
    ids["lastfm_track"] = entity_keys["track"]

    ids["lastfm_tag_edge"] = {
        f"{edge.entity_kind}\x1f{entity_id}\x1f{edge.tag_name_norm}": position
        for position, (edge, entity_id) in enumerate(_ordered_edges(result, entity_keys), start=1)
    }
    ids["lastfm_similarity"] = {
        f"{similarity.artist_identity}\x1f{similarity.peer_name_norm}": position
        for position, similarity in enumerate(_ordered_similarities(result), start=1)
    }
    ids["lastfm_artist_alias"] = {
        alias.requested_name_norm: position
        for position, alias in enumerate(_ordered_aliases(result), start=1)
    }
    ids["lastfm_entity_tag"] = {
        norm: position
        for position, (_, norm, _) in enumerate(derive_entity_tag_counts(result.tag_edges), start=1)
    }
    return ids


def _ordered_edges(
    result: DerivationResult, entity_keys: Mapping[str, Mapping[str, int]]
) -> list[tuple[Any, int]]:
    """Tag edges in a fixed order, paired with their entity id."""
    return [
        (edge, entity_keys[edge.entity_kind][edge.entity_identity])
        for edge in sorted(
            result.tag_edges,
            key=lambda item: (
                item.entity_kind,
                entity_keys[item.entity_kind][item.entity_identity],
                item.tag_name_norm,
            ),
        )
    ]


def _ordered_similarities(result: DerivationResult) -> list[Any]:
    return sorted(
        result.similarities, key=lambda item: (item.artist_identity, item.rank, item.peer_name_norm)
    )


def _ordered_aliases(result: DerivationResult) -> list[Any]:
    return sorted(result.aliases, key=lambda item: item.requested_name_norm)


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


def validate(result: DerivationResult, observations: Sequence[RawObservation]) -> None:
    """Refuse to write a derivation that could not have come from the raw layer.

    Runs before anything is written, so a failure leaves the derived tables exactly
    as they were. See ``docs/design/0003`` §7 step 2.
    """
    known_responses = {observation.response_id for observation in observations}

    for entity in result.entities():
        if not entity.identity:
            raise ReindexError(f"{entity.kind} entity has an empty identity")
        if not entity.latest_response_id:
            raise ReindexError(f"{entity.kind} {entity.identity!r} has no latest_response_id")
        if entity.latest_response_id not in known_responses:
            raise ReindexError(
                f"{entity.kind} {entity.identity!r} points at response "
                f"{entity.latest_response_id!r}, which is not in the raw layer"
            )
        if entity.first_seen_at is None or entity.last_seen_at is None:
            raise ReindexError(f"{entity.kind} {entity.identity!r} has no observation timestamps")
        if entity.first_seen_at > entity.last_seen_at:
            raise ReindexError(
                f"{entity.kind} {entity.identity!r} has first_seen_at after last_seen_at"
            )

    identifiers = {
        kind: {entity.identity for entity in entities}
        for kind, entities in (
            ("artist", result.artists),
            ("album", result.albums),
            ("track", result.tracks),
        )
    }

    for edge in result.tag_edges:
        if edge.entity_identity not in identifiers.get(edge.entity_kind, set()):
            raise ReindexError(
                f"tag edge for {edge.entity_kind} {edge.entity_identity!r} "
                "does not resolve to a derived entity"
            )
        if not edge.tag_name_norm:
            raise ReindexError("tag edge has an empty normalised tag name")
        if edge.latest_response_id not in known_responses:
            raise ReindexError("tag edge points at an unknown response")

    for similarity in result.similarities:
        if similarity.artist_identity not in identifiers["artist"]:
            raise ReindexError(
                f"similarity for {similarity.artist_identity!r} does not resolve to an artist"
            )
        if not similarity.peer_name_norm:
            raise ReindexError("similarity has an empty peer name")

    for alias in result.aliases:
        if alias.canonical_identity not in identifiers["artist"]:
            raise ReindexError(
                f"alias {alias.requested_name_norm!r} points at unknown artist "
                f"{alias.canonical_identity!r}"
            )

    duplicates = _duplicate_identities(result)
    if duplicates:
        raise ReindexError(f"duplicate derived identities: {sorted(duplicates)[:5]}")


def _duplicate_identities(result: DerivationResult) -> set[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for entity in result.entities():
        key = f"{entity.kind}:{entity.identity}"
        if key in seen:
            duplicates.add(key)
        seen.add(key)
    return duplicates


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #


async def _truncate_derived(session: AsyncSession) -> None:
    """Clear the derived tables inside the caller's transaction."""
    # One statement so the tables are locked and cleared together; TRUNCATE is
    # transactional in Postgres, so a later failure rolls all of it back.
    await session.execute(text(f"TRUNCATE TABLE {', '.join(DERIVED_TABLES)}"))


async def _insert_entities(
    session: AsyncSession,
    kind: str,
    entities: Sequence[DerivedEntity],
    ids: Mapping[str, int],
) -> None:
    if not entities:
        return
    model = ENTITY_MODELS[kind]
    rows: list[dict[str, Any]] = []
    for entity in entities:
        columns = entity.as_columns()
        columns["id"] = ids[entity.identity]
        rows.append(columns)
    await session.execute(insert(model), rows)


async def _insert_edges(
    session: AsyncSession, result: DerivationResult, ids: Mapping[str, Mapping[str, int]]
) -> None:
    """Tag edges, similarity, aliases and the tag aggregate, with explicit ids."""
    entity_ids = {
        "artist": ids["lastfm_artist"],
        "album": ids["lastfm_album"],
        "track": ids["lastfm_track"],
    }
    edge_ids = ids["lastfm_tag_edge"]
    edge_rows: list[dict[str, Any]] = []
    for edge, entity_id in _ordered_edges(result, entity_ids):
        key = f"{edge.entity_kind}\x1f{entity_id}\x1f{edge.tag_name_norm}"
        edge_rows.append(
            {
                "id": edge_ids[key],
                "entity_kind": edge.entity_kind,
                "entity_id": entity_id,
                "tag_name": edge.tag_name,
                "tag_name_norm": edge.tag_name_norm,
                "rank": edge.rank,
                "count": edge.count,
                "observed_at": edge.observed_at,
                "latest_response_id": edge.latest_response_id,
            }
        )
    if edge_rows:
        await session.execute(insert(LastfmTagEdge), edge_rows)

    names_to_identity = {
        entity.name_norm: entity.identity for entity in result.artists if entity.name_norm
    }
    similarity_ids = ids["lastfm_similarity"]
    similarity_rows: list[dict[str, Any]] = []
    for similarity in _ordered_similarities(result):
        peer_identity = names_to_identity.get(similarity.peer_name_norm)
        key = f"{similarity.artist_identity}\x1f{similarity.peer_name_norm}"
        similarity_rows.append(
            {
                "id": similarity_ids[key],
                "artist_id": entity_ids["artist"][similarity.artist_identity],
                "peer_name": similarity.peer_name,
                "peer_name_norm": similarity.peer_name_norm,
                "peer_mbid": similarity.peer_mbid,
                "peer_artist_id": (
                    entity_ids["artist"].get(peer_identity) if peer_identity else None
                ),
                "match": similarity.match,
                "rank": similarity.rank,
                "observed_at": similarity.observed_at,
                "latest_response_id": similarity.latest_response_id,
            }
        )
    if similarity_rows:
        await session.execute(insert(LastfmSimilarity), similarity_rows)

    alias_ids = ids["lastfm_artist_alias"]
    alias_rows = [
        {
            "id": alias_ids[alias.requested_name_norm],
            "requested_name_norm": alias.requested_name_norm,
            "canonical_artist_id": entity_ids["artist"][alias.canonical_identity],
            "observed_at": alias.observed_at,
        }
        for alias in _ordered_aliases(result)
        if alias.canonical_identity in entity_ids["artist"]
    ]
    if alias_rows:
        await session.execute(insert(LastfmArtistAlias), alias_rows)

    tag_ids = ids["lastfm_entity_tag"]
    tag_rows = [
        {"id": tag_ids[norm], "tag_name": display, "tag_name_norm": norm, "entity_count": count}
        for display, norm, count in derive_entity_tag_counts(result.tag_edges)
    ]
    if tag_rows:
        await session.execute(insert(LastfmEntityTag), tag_rows)


async def _reset_sequences(session: AsyncSession) -> None:
    """Move each identity sequence past the explicit ids we inserted.

    Ids are assigned explicitly so a rebuild is reproducible. Without this, the
    sequence would still be at 1 and the first future insert would collide.
    """
    for table in DERIVED_TABLES:
        await session.execute(
            text(
                "SELECT setval(pg_get_serial_sequence(:table, 'id'), "
                "COALESCE((SELECT max(id) FROM " + table + "), 1))"
            ),
            {"table": table},
        )


async def count_rows(session: AsyncSession) -> dict[str, int]:
    """Row counts per derived table, for reporting and comparison."""
    counts: dict[str, int] = {}
    for table in DERIVED_TABLES:
        counts[table] = int(await session.scalar(text(f"SELECT count(*) FROM {table}")) or 0)
    return counts


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


async def reindex(
    session: AsyncSession,
    *,
    dry_run: bool = False,
    since: str | None = None,
    only: str | None = None,
) -> ReindexReport:
    """Re-derive the archive tables from the raw layer.

    ``dry_run`` derives and validates, then reports the change against the current
    tables without writing anything.
    """
    started = _monotonic_ms()
    since_dt = _parse_since(since)
    if since_dt is not None:
        # The design's --since path is an optimisation; a full rebuild is always
        # correct, so rather than reimplementing the derivation incrementally (and
        # risking the two paths diverging) we rebuild and report the window. This
        # is honest: the report says what was actually derived from.
        log.info("reindex_since_requested", since=since_dt.isoformat())

    observations = await load_observations(session)
    owners = await load_similarity_owners(session)
    corrections = await load_alias_corrections(session)

    result = derive_all(observations, alias_corrections=corrections, similarity_owners=owners)
    if only:
        result = _filter_only(result, only)

    validate(result, observations)
    ids = assign_ids(result)

    report = ReindexReport(
        dry_run=dry_run,
        artists=len(result.artists),
        albums=len(result.albums),
        tracks=len(result.tracks),
        tag_edges=len(result.tag_edges),
        similarities=len(result.similarities),
        aliases=len(result.aliases),
        entity_tags=len(derive_entity_tag_counts(result.tag_edges)),
        observations=len(observations),
        response_bodies=len({observation.response_id for observation in observations}),
        unexpected_shapes=result.skipped_unrecognised_shape,
        expected_no_envelope=result.skipped_expected_no_envelope,
    )

    if dry_run:
        report.changes = await _diff(session, result, ids)
    else:
        await _truncate_derived(session)
        await _insert_entities(session, "artist", result.artists, ids["lastfm_artist"])
        await _insert_entities(session, "album", result.albums, ids["lastfm_album"])
        await _insert_entities(session, "track", result.tracks, ids["lastfm_track"])
        await _insert_edges(session, result, ids)
        await _reset_sequences(session)
        report.changes = {}

    report.duration_ms = _monotonic_ms() - started
    log.info("reindex_complete", dry_run=dry_run, **report.as_dict()["counts"])
    return report


def _filter_only(result: DerivationResult, only: str) -> DerivationResult:
    """Restrict a derivation to one table family, for a targeted rebuild."""
    if only == "artist":
        return DerivationResult(artists=result.artists)
    if only == "album":
        return DerivationResult(albums=result.albums)
    if only == "track":
        return DerivationResult(tracks=result.tracks)
    if only in {"tag", "similar", "alias"}:
        return result
    raise ReindexError(
        f"unknown --only target {only!r}; expected artist, album, track, tag, similar or alias"
    )


async def _diff(
    session: AsyncSession, result: DerivationResult, ids: Mapping[str, Mapping[str, int]]
) -> dict[str, Any]:
    """Row-level difference between the derivation and the current tables."""
    current = await count_rows(session)
    derived = {
        "lastfm_artist": len(result.artists),
        "lastfm_album": len(result.albums),
        "lastfm_track": len(result.tracks),
        "lastfm_tag_edge": len(result.tag_edges),
        "lastfm_similarity": len(result.similarities),
        "lastfm_artist_alias": len(result.aliases),
        "lastfm_entity_tag": len(derive_entity_tag_counts(result.tag_edges)),
    }
    changed = {
        table: {"current": current.get(table, 0), "derived": derived[table]}
        for table in DERIVED_TABLES
        if current.get(table, 0) != derived[table]
    }

    # A count that matches can still hide a changed value, so compare content too.
    for kind, model in ENTITY_MODELS.items():
        mismatches = await _entity_value_mismatches(session, kind, model, result, ids)
        if mismatches:
            changed[f"{kind}_values"] = {"changed_rows": mismatches}

    return {
        "identical": not changed,
        "tables": changed,
        "derived_counts": derived,
        "current_counts": current,
    }


async def _entity_value_mismatches(
    session: AsyncSession,
    kind: str,
    model: Any,
    result: DerivationResult,
    ids: Mapping[str, Mapping[str, int]],
) -> int:
    """How many entity rows would change value, not just count."""
    entities = {
        "artist": result.artists,
        "album": result.albums,
        "track": result.tracks,
    }[kind]
    entity_ids = ids[f"lastfm_{kind}"]
    rows = (await session.execute(select(model.id, model.identity, model.latest_response_id))).all()
    existing = {row.identity: (int(row.id), row.latest_response_id) for row in rows}
    mismatches = 0
    for entity in entities:
        expected_id = entity_ids[entity.identity]
        present = existing.get(entity.identity)
        if present is None or present[0] != expected_id or present[1] != entity.latest_response_id:
            mismatches += 1
    return mismatches


def _parse_since(value: str | None) -> datetime | None:
    if not value:
        return None
    text_value = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text_value)
    except ValueError as exc:
        raise ReindexError(f"--since is not an ISO date: {value!r}") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _monotonic_ms() -> int:
    import time

    return int(time.monotonic() * 1000)


def watermark(now: datetime | None = None, *, days: int = 0) -> datetime:
    """Convenience for callers that want a ``since`` value rather than a window."""
    base = now or datetime.now(UTC)
    return base - timedelta(days=days)


async def describe_archive(settings: Settings, *, limit: int = 20) -> dict[str, Any]:
    """Summarise what is archived and what the derivation would make of it.

    Runs the derivation in memory without writing, so it is safe at any time. The
    interesting part is ``unrecognised``: archived bodies that carry no artist,
    album or track envelope, or that had nothing to key on. Those are invisible in
    the entity counts, so they are listed explicitly.
    """
    init_engine(settings)
    factory = get_session_factory()
    async with factory() as session:
        observations = await load_observations(session)
        owners = await load_similarity_owners(session)
        corrections = await load_alias_corrections(session)
        current = await count_rows(session)

    result = derive_all(observations, alias_corrections=corrections, similarity_owners=owners)

    by_method: dict[str, int] = {}
    for observation in observations:
        by_method[observation.method] = by_method.get(observation.method, 0) + 1

    # Which bodies produced nothing. Deliberately driven by the derivation's own
    # output rather than by inspecting envelopes: a body can carry an {"artist": ...}
    # envelope and still be unusable, and listing by envelope would hide exactly the
    # rows an operator needs to see.
    produced = {entity.latest_response_id for entity in result.entities()}
    unexpected_shapes: dict[str, int] = {}
    for observation in observations:
        if observation.response_id in produced:
            continue
        if observation.method in METHODS_WITHOUT_ENTITY_ENVELOPE:
            continue
        key = f"{observation.method}: {','.join(sorted(observation.body)) or '<empty>'}"
        unexpected_shapes[key] = unexpected_shapes.get(key, 0) + 1

    return {
        "raw": {
            "observations_used": len(observations),
            "distinct_bodies": len({o.response_id for o in observations}),
            "by_method": dict(sorted(by_method.items())),
        },
        "derived_now": current,
        "would_derive": {
            "artists": len(result.artists),
            "albums": len(result.albums),
            "tracks": len(result.tracks),
            "tag_edges": len(result.tag_edges),
            "similarities": len(result.similarities),
            "aliases": len(result.aliases),
            "entity_tags": len(derive_entity_tag_counts(result.tag_edges)),
        },
        "shapes": {
            "unexpected": result.skipped_unrecognised_shape,
            "expected_no_envelope": result.skipped_expected_no_envelope,
            "note": (
                "'unexpected' counts archived bodies with no artist/album/track envelope "
                "that were not expected to lack one -- zero is healthy, and a rise means "
                "lastfm's response shape has drifted or a method was archived that the "
                "derivation does not model. 'expected_no_envelope' counts methods that "
                "legitimately carry another shape."
            ),
            "unexpected_bodies": dict(
                sorted(unexpected_shapes.items(), key=lambda item: -item[1])[: max(limit, 0)]
            ),
        },
    }


async def derived_summary(session: AsyncSession) -> dict[str, Any]:
    """Counts plus the newest observation time, for the stats endpoint."""
    counts = await count_rows(session)
    newest = await session.scalar(
        select(func.max(LastfmRequest.requested_at)).join(
            LastfmResponse, LastfmResponse.id == LastfmRequest.response_id
        )
    )
    return {
        "counts": counts,
        "total": sum(counts.values()),
        "newest_observation_at": newest.isoformat() if newest else None,
    }


# Kept importable so callers can avoid a second import of the models module.
__all__ = [
    "DERIVED_TABLES",
    "ReindexError",
    "ReindexReport",
    "assign_ids",
    "count_rows",
    "derived_summary",
    "describe_archive",
    "load_alias_corrections",
    "load_observations",
    "load_similarity_owners",
    "reindex",
    "reindex_with_settings",
    "validate",
    "watermark",
]


async def reindex_with_settings(
    settings: Settings,
    *,
    dry_run: bool = False,
    since: str | None = None,
    only: str | None = None,
) -> ReindexReport:
    """Open a session, run a reindex, commit on success.

    The CLI and the HTTP endpoint both want this shape; keeping it here means
    neither has to know how a session is constructed.
    """
    init_engine(settings)
    factory = get_session_factory()
    async with factory() as session:
        report = await reindex(session, dry_run=dry_run, since=since, only=only)
        if dry_run:
            await session.rollback()
        else:
            await session.commit()
    return report
