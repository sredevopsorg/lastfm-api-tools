"""Item snapshots: the canonical view of an item's writable state.

Everything entering or leaving Jellyfin passes through here. Normalising in one
place buys three things that would otherwise be smeared across the codebase:

* **Round-trip fidelity.** ``None`` and ``[]`` are different states. "Unknown"
  (the server did not send the key) is preserved separately, so a field the
  server omits is never mistaken for an empty one -- and an empty untouched field
  is never mistaken for an instruction to clear it.
* **Comparable types.** ``PremiereDate`` arrives as a datetime but must be sent
  as an ISO string; ``CommunityRating`` may be int or float. It stays a datetime
  in ``fields``, which is what makes a date comparison meaningful -- and means
  ``fields`` is *not* directly JSON-serialisable, so anything putting it in a
  response body has to convert it first (see ``api/serialization``).
* **One build site for write payloads.** ``to_payload`` emits exactly
  ``payload_fields(kind)`` (ADR 0003), so the completeness invariant is a
  property of a single function.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog
from pydantic import BaseModel, ConfigDict

from metaedit.domain.writable import (
    ItemKind,
    is_editable,
    payload_fields,
    payload_matches_kind,
)

log = structlog.get_logger(__name__)

# Fields whose empty value is an empty list rather than null.
_LIST_FIELDS = frozenset(
    {"Genres", "Tags", "Studios", "ProductionLocations", "ExternalUrls", "People", "LockedFields"}
)
# Fields whose empty value is an empty object.
_DICT_FIELDS = frozenset({"ProviderIds"})


class NormalizedItem(BaseModel):
    """A whitelist-shaped view of one Jellyfin item."""

    model_config = ConfigDict(extra="forbid")

    kind: ItemKind
    item_id: str
    name: str
    etag: str | None = None
    date_last_saved: datetime | None = None
    source_type: str | None = None
    location_type: str | None = None
    is_folder: bool | None = None
    path: str | None = None
    # Server state we display but never edit.
    album: str | None = None
    album_id: str | None = None
    artist_names: list[str] | None = None
    album_artist_names: list[str] | None = None

    fields: dict[str, Any]

    def get(self, field: str, default: Any = None) -> Any:
        return self.fields.get(field, default)

    def as_state(self) -> dict[str, Any]:
        """What the API returns for ``GET /items/{id}/state``."""
        dto = {
            "SourceType": self.source_type,
            "LocationType": self.location_type,
            "Type": self.kind,
        }
        editable, reason = is_editable(dto)
        return {
            "item_id": self.item_id,
            "kind": self.kind,
            "name": self.name,
            "etag": self.etag,
            "date_last_saved": self.date_last_saved.isoformat() if self.date_last_saved else None,
            "editable": editable,
            "not_editable_reason": reason,
            "locked": bool(self.fields.get("LockData")),
            "locked_fields": list(self.fields.get("LockedFields") or []),
            "album": self.album,
            "album_id": self.album_id,
            "artist_names": list(self.artist_names or []),
            "album_artist_names": list(self.album_artist_names or []),
            "fields": self.fields,
        }


class NormalizedItemValidationError(ValueError):
    """Raised when a partial payload is offered to ``to_payload``."""


def from_dto(dto: dict[str, Any], kind: ItemKind) -> NormalizedItem:
    """Build a snapshot from a raw Jellyfin item payload.

    Every field the media type requires is present in ``fields``: the read
    requests all of them explicitly, and a key the server still omitted is filled
    with that field's empty value. A complete snapshot is what makes
    ``to_payload`` safe -- it can never send a short body.
    """
    fields: dict[str, Any] = {}
    for field in payload_fields(kind):
        if field in dto:
            fields[field] = _normalize(field, dto.get(field))
        else:
            # Unexpected: we request every whitelist field. Filling the empty
            # value keeps the payload complete rather than silently short.
            log.warning("jellyfin_field_missing", field=field, item_id=dto.get("Id"), kind=kind)
            fields[field] = empty_value(field)

    return NormalizedItem(
        kind=kind,
        item_id=str(dto.get("Id") or ""),
        name=str(dto.get("Name") or ""),
        etag=dto.get("Etag"),
        date_last_saved=_as_datetime(dto.get("DateLastSaved")),
        source_type=dto.get("SourceType"),
        location_type=dto.get("LocationType"),
        is_folder=dto.get("IsFolder"),
        path=dto.get("Path"),
        album=dto.get("Album"),
        album_id=dto.get("AlbumId"),
        artist_names=_credited_artists(dto),
        album_artist_names=_album_artists(dto),
        fields=fields,
    )


def from_snapshot_row(row: dict[str, Any]) -> NormalizedItem:
    """Rebuild a snapshot from a stored ``snapshot`` row (for revert).

    Robust to rows written before a field existed: a missing entry becomes that
    field's empty value, and ``to_payload`` still emits a complete body.
    """
    kind: ItemKind = row["kind"]
    stored: dict[str, Any] = row.get("fields") or {}
    fields: dict[str, Any] = {}
    for field in payload_fields(kind):
        fields[field] = _normalize(field, stored.get(field, empty_value(field)))
    return NormalizedItem(
        kind=kind,
        item_id=row["item_id"],
        name=row.get("name") or "",
        etag=row.get("etag"),
        date_last_saved=row.get("date_last_saved"),
        fields=fields,
    )


def to_payload(base: NormalizedItem, changes: dict[str, Any]) -> dict[str, Any]:
    """Build a complete write payload: current values with ``changes`` layered on.

    Raises ``NormalizedItemValidationError`` if the result is not exactly the
    field set the media type requires (ADR 0003).
    """
    payload: dict[str, Any] = {}
    for field in payload_fields(base.kind):
        if field in changes:
            payload[field] = _to_wire(field, changes[field])
        elif field in base.fields:
            payload[field] = base.fields[field]
        else:
            payload[field] = empty_value(field)

    if not payload_matches_kind(payload, base.kind):
        missing = sorted(set(payload_fields(base.kind)) - set(payload))
        extra = sorted(set(payload) - set(payload_fields(base.kind)))
        msg = (
            "refusing to send an incomplete item update: the server treats a "
            f"missing field as null. kind={base.kind} missing={missing} extra={extra}"
        )
        raise NormalizedItemValidationError(msg)
    return payload


def snapshot_fields(base: NormalizedItem) -> dict[str, Any]:
    """The JSONB body persisted in the ``snapshot`` table.

    Every value is passed through ``_to_wire``, because this body is stored in a
    JSONB column and JSON has no datetime. ``fields`` holds ``PremiereDate`` as a
    datetime deliberately -- that is what makes a date comparison meaningful, and
    ``to_payload`` already converts it on the way to Jellyfin -- but a datetime
    handed to a JSONB column is a ``TypeError`` at insert time.

    That failure lands in the worst possible place: after the change set has been
    reviewed and confirmed, during the snapshot flush, before anything is written.
    The user gets an internal error on a write they believed was approved, and no
    snapshot exists to explain it. Measured on the live library, 5,916 of 6,583
    items (470 of 502 albums, 5,437 of 5,442 songs) carry a ``PremiereDate`` and
    would fail this way; artists mostly do not, which is why the first report came
    from an artist that happened to have one.
    """
    return {
        field: _to_wire(field, base.fields.get(field, empty_value(field)))
        for field in payload_fields(base.kind)
    }


def empty_value(field: str) -> Any:
    """What "no value" means for a field, in the shape the server expects.

    Applied identically to the read path, the persisted snapshot and the write
    payload, so all three agree on what an empty field looks like.
    """
    if field in _LIST_FIELDS:
        return []
    if field in _DICT_FIELDS:
        return {}
    if field == "LockData":
        return False
    return None


# --------------------------------------------------------------------- helpers


def _normalize(field: str, value: Any) -> Any:
    if field in {"Genres", "Tags", "ProductionLocations", "LockedFields"}:
        return [str(item) for item in (value or [])]
    if field == "Studios":
        return [
            {"Name": pair.get("Name"), "Id": pair.get("Id")}
            for pair in (value or [])
            if isinstance(pair, dict)
        ]
    if field == "ProviderIds":
        return {str(key): str(val) for key, val in (value or {}).items() if val}
    if field == "ExternalUrls":
        return [
            {"Name": url.get("Name"), "Url": url.get("Url")}
            for url in (value or [])
            if isinstance(url, dict)
        ]
    if field == "People":
        return list(value or [])
    if field == "PremiereDate":
        # Stored as a datetime so comparisons against a local date work, and
        # converted back on the way out by `_to_wire`.
        return _as_datetime(value)
    if field in {"CommunityRating", "CriticRating"}:
        return None if value is None else float(value)
    if field == "ProductionYear":
        return None if value is None else int(value)
    if field == "LockData":
        return bool(value) if value is not None else False
    return value


def _to_wire(field: str, value: Any) -> Any:
    if field == "PremiereDate" and isinstance(value, datetime):
        return value.isoformat()
    return value


def _as_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _names(pairs: Any) -> list[str]:
    if not isinstance(pairs, list):
        return []
    return [str(pair["Name"]) for pair in pairs if isinstance(pair, dict) and pair.get("Name")]


def _credited_artists(dto: dict[str, Any]) -> list[str]:
    """The item's own artist names, from whichever spelling the server sent.

    Jellyfin is inconsistent here: songs carry ``ArtistItems`` (structured pairs),
    albums may carry ``Artists`` (plain strings) instead, and older responses carry
    neither. Reading only one spelling silently yields no artist, and an album with no
    artist cannot be looked up on Last.fm at all.
    """
    from_pairs = _names(dto.get("ArtistItems"))
    if from_pairs:
        return from_pairs
    plain = dto.get("Artists")
    if isinstance(plain, list):
        return [str(name) for name in plain if name]
    return []


def _album_artists(dto: dict[str, Any]) -> list[str]:
    """The album artist, ordered by specificity: pairs, then scalar, then plain list."""
    from_pairs = _names(dto.get("AlbumArtists"))
    if from_pairs:
        return from_pairs
    scalar = dto.get("AlbumArtist")
    if isinstance(scalar, str) and scalar.strip():
        return [scalar.strip()]
    plain = dto.get("Artists")
    if isinstance(plain, list):
        return [str(name) for name in plain if name]
    return []
