"""Building a mapping candidate from an archived entity.

The bridge between phase 3 (the derived archive) and phase 4 (the pure mapping
layer). It takes a derived row and produces the value object the mapper expects,
which keeps the archive's storage shape out of the mapping logic and makes the whole
edit path testable without a database.

Nothing here decides anything: the row already resolved the ambiguity (which artist,
which album, which spelling) when it was derived, and the confidence score decides
whether to trust it.
"""

from __future__ import annotations

from typing import Any

from metaedit.domain.mapping import Candidate
from metaedit.domain.tags import TagInput
from metaedit.domain.writable import ItemKind

_KIND_BY_TABLE: dict[str, ItemKind] = {
    "artist": "MusicArtist",
    "album": "MusicAlbum",
    "track": "Audio",
}


def kind_for_entity(entity_kind: str) -> ItemKind:
    """Map the archive's entity kind onto a Jellyfin media type."""
    try:
        return _KIND_BY_TABLE[entity_kind]
    except KeyError as exc:
        msg = f"unknown entity kind {entity_kind!r}; expected one of {sorted(_KIND_BY_TABLE)}"
        raise ValueError(msg) from exc


def tags_from_derived(value: Any) -> list[TagInput]:
    """Read the stored tag list into the mapper's input shape.

    The derived row stores ``[{name, count, url}]`` with ``count`` already null where
    Last.fm supplied none, so the "no count is not a count of zero" distinction is
    preserved rather than re-derived here.
    """
    if not isinstance(value, list):
        return []
    result: list[TagInput] = []
    for entry in value:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        count = entry.get("count")
        result.append(TagInput(name=name.strip(), count=count if isinstance(count, int) else None))
    return result


def candidate_from_entity(entity: Any, *, kind: str) -> Candidate:
    """Build the mapper's candidate from a derived entity row.

    ``entity`` is duck-typed rather than declared as the ORM model so the edit path
    can be tested with a plain object, and so this module does not depend on the
    database layer.
    """
    return Candidate(
        kind=kind_for_entity(kind),
        name=getattr(entity, "name", "") or "",
        mbid=getattr(entity, "mbid", None),
        artist=getattr(entity, "artist_name", None),
        # Only albums carry a year, and live-verified it cannot be sourced from
        # album.getInfo at all -- so this is normally None rather than wrong.
        year=getattr(entity, "production_year", None),
        duration_ms=getattr(entity, "duration_ms", None),
        # Verbatim: sanitising is the mapping layer's job.
        overview=getattr(entity, "overview", None),
        tags=tags_from_derived(getattr(entity, "tags", None)),
        url=getattr(entity, "url", None),
        listeners=getattr(entity, "listeners", None),
        playcount=getattr(entity, "playcount", None),
        # Provenance, so a written value traces back to the archived response.
        response_id=getattr(entity, "latest_response_id", None),
        request_ids=([entity.last_request_id] if getattr(entity, "last_request_id", None) else []),
    )
