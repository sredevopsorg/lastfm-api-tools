"""Resolving a Jellyfin item to archived Last.fm candidates.

Reads the derived layer only. Nothing here touches the network: if we have already
fetched an entity, matching it is a local lookup; if we have not, the result is an
empty list and fetching is a separate, explicit decision.

Matching prefers the MusicBrainz id when the item has one, because that is exact
rather than inferred. Name matching is the fallback, and it is deliberately generous
-- returning several plausible candidates for a human to choose between is better
than returning one wrong one with a confident-looking score.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from metaedit.adapters.lastfm.canonical import normalize_name
from metaedit.db.models import LastfmAlbum, LastfmArtist, LastfmTrack
from metaedit.domain.confidence import CandidateIdentity, Confidence, MatchContext, score
from metaedit.domain.snapshot import NormalizedItem
from metaedit.domain.writable import ItemKind

# Which ProviderIds key carries the MusicBrainz id for each media type.
_MBID_KEYS: dict[ItemKind, str] = {
    "MusicArtist": "MusicBrainzArtist",
    "MusicAlbum": "MusicBrainzAlbum",
    "Audio": "MusicBrainzTrack",
}

_MODELS: dict[ItemKind, Any] = {
    "MusicArtist": LastfmArtist,
    "MusicAlbum": LastfmAlbum,
    "Audio": LastfmTrack,
}


@dataclass(frozen=True, slots=True)
class ResolvedCandidate:
    """An archived entity plus how well it matches the item."""

    entity: Any
    kind: ItemKind
    confidence: Confidence
    # True when the match came from an exact MusicBrainz id rather than a name.
    from_mbid: bool

    @property
    def entity_kind(self) -> str:
        return {"MusicArtist": "artist", "MusicAlbum": "album", "Audio": "track"}[self.kind]


def match_context(item: NormalizedItem) -> MatchContext:
    """What we know about the item, in the scorer's shape."""
    provider_ids = item.get("ProviderIds") or {}
    return MatchContext(
        kind=item.kind,
        name=item.name,
        mbid=provider_ids.get(_MBID_KEYS[item.kind]),
        artists=[*(item.artist_names or []), *(item.album_artist_names or [])],
        year=item.get("ProductionYear"),
        duration_ms=_duration_ms(item),
    )


def candidate_identity(entity: Any) -> CandidateIdentity:
    """What the archived entity claims to be, in the scorer's shape."""
    return CandidateIdentity(
        name=getattr(entity, "name", "") or "",
        mbid=getattr(entity, "mbid", None),
        artist=getattr(entity, "artist_name", None),
        year=getattr(entity, "production_year", None),
        duration_ms=getattr(entity, "duration_ms", None),
    )


async def resolve_candidates(
    session: AsyncSession,
    item: NormalizedItem,
    *,
    limit: int = 5,
) -> list[ResolvedCandidate]:
    """Find archived entities that could be this item, best first.

    The MBID path returns at most one result because an id either matches or does not;
    the name path returns several so a human can choose when confidence is low.
    """
    model = _MODELS[item.kind]
    context = match_context(item)

    if context.mbid:
        exact = (
            await session.execute(select(model).where(model.mbid == context.mbid).limit(1))
        ).scalar_one_or_none()
        if exact is not None:
            return [
                ResolvedCandidate(
                    entity=exact,
                    kind=item.kind,
                    confidence=score(context, candidate_identity(exact)),
                    from_mbid=True,
                )
            ]

    if not normalize_name(item.name):
        return []

    statement = select(model).where(model.name_norm == normalize_name(item.name))
    if item.kind in {"MusicAlbum", "Audio"}:
        # Constrain by credited artist where the item knows one, so "Greatest Hits"
        # does not resolve to every artist's compilation of that name.
        names = [normalize_name(name) for name in context.artists if normalize_name(name)]
        if names:
            artist_clauses = [model.artist_name_norm == name for name in names]
            statement = statement.where(or_(*artist_clauses))
    rows = (await session.execute(statement.limit(max(limit, 1) * 3))).scalars().all()

    resolved = [
        ResolvedCandidate(
            entity=row,
            kind=item.kind,
            confidence=score(context, candidate_identity(row)),
            from_mbid=False,
        )
        for row in rows
    ]
    # Best first, then by identity so equal scores order reproducibly.
    resolved.sort(key=lambda candidate: (-candidate.confidence.total, candidate.entity.identity))
    return resolved[:limit]


def _duration_ms(item: NormalizedItem) -> int | None:
    """Duration in milliseconds, converted from Jellyfin's 100-nanosecond ticks."""
    ticks = item.get("RunTimeTicks")
    if not isinstance(ticks, int) or ticks <= 0:
        return None
    return ticks // 10_000


def describe_candidate(candidate: ResolvedCandidate) -> dict[str, Any]:
    """Display-safe summary, including enough to explain the score."""
    entity = candidate.entity
    return {
        "entity_kind": candidate.entity_kind,
        "entity_id": getattr(entity, "id", None),
        "identity": getattr(entity, "identity", None),
        "name": getattr(entity, "name", None),
        "mbid": getattr(entity, "mbid", None),
        "artist": getattr(entity, "artist_name", None),
        "year": getattr(entity, "production_year", None),
        "duration_ms": getattr(entity, "duration_ms", None),
        "url": getattr(entity, "url", None),
        "listeners": getattr(entity, "listeners", None),
        "playcount": getattr(entity, "playcount", None),
        "tags": [
            {"name": entry.get("name"), "count": entry.get("count")}
            for entry in (getattr(entity, "tags", None) or [])
            if isinstance(entry, dict)
        ],
        "has_overview": bool(getattr(entity, "overview", None)),
        "last_seen_at": (
            entity.last_seen_at.isoformat() if getattr(entity, "last_seen_at", None) else None
        ),
        "latest_response_id": getattr(entity, "latest_response_id", None),
        "matched_on": "mbid" if candidate.from_mbid else "name",
        "confidence": {
            "total": candidate.confidence.total,
            "verdict": candidate.confidence.verdict,
            "accepts_by_default": candidate.confidence.accepts_by_default,
            "mbid_conflict": candidate.confidence.mbid_conflict,
            "components": [
                {
                    "name": component.name,
                    "value": component.value,
                    "applied_weight": round(component.applied_weight, 4),
                    "detail": component.detail,
                }
                for component in candidate.confidence.components
            ],
            "notes": candidate.confidence.notes,
        },
    }
