"""Turning domain values into something a JSON encoder will accept.

FastAPI applies ``jsonable_encoder`` to whatever a route *returns*, but an exception
handler builds its response directly -- ``JSONResponse(content=...)`` runs plain
``json.dumps``, which knows nothing about ``datetime``. Every error body therefore had
a latent 500: as soon as a detail carried a timestamp, the failure reporting mechanism
became a second failure, and the client saw a bare "Internal Server Error" with the
status of whatever went wrong.

The case that reached users was a song with a ``PremiereDate``. Songs carry one far more
often than artists or albums, which is why it showed up while editing songs.

``to_jsonable`` is the single answer to "can this go in a response body", applied where
the body is built rather than at each site that might put a value in one.
"""

from __future__ import annotations

import dataclasses
from datetime import date, datetime, time
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import UUID

# Types with an unambiguous string form. Kept as separate checks rather than one tuple
# because UUID and Path have no `isoformat`, and the distinction is the point: these
# three spell a moment in time, and JSON has no type for one.
_TEMPORAL_TYPES = (datetime, date, time)


def to_jsonable(value: Any) -> Any:
    """Recursively convert ``value`` into JSON-serialisable primitives.

    Mirrors FastAPI's ``jsonable_encoder`` for the types this project actually uses,
    which is a shorter list than FastAPI has to cover: datetimes, enums, dataclasses,
    mappings and sequences. Anything unrecognised is returned unchanged rather than
    stringified, so a mistake shows up as a 500 in testing instead of as a silently
    mangled value in production.
    """
    # Fast path for the overwhelmingly common cases, before any isinstance chain.
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, _TEMPORAL_TYPES):
        return value.isoformat()
    if isinstance(value, UUID | Path):
        return str(value)
    if isinstance(value, Enum):
        return to_jsonable(value.value)
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [to_jsonable(item) for item in value]
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: to_jsonable(getattr(value, f.name)) for f in dataclasses.fields(value)}
    # Pydantic models expose this; calling it keeps a nested model's own field
    # serialisation rules rather than reaching into its internals.
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return to_jsonable(dump(mode="json"))
    return value
