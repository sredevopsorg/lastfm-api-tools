"""What an exclusion pattern is allowed to see, per item shape.

The rule is the domain's (``exclusion.LABEL_SOURCES``): the item's name, the album it is
on, and who it is by. What differs per boundary is only where those three strings live --
the API's ``ItemSummary`` keeps the album artist as one string, a Jellyfin DTO may spell it
three different ways -- so the reading lives here, at the boundary, and the matching stays
in ``domain.exclusion``.

Both adapters are in one file on purpose. Two implementations of "which fields a pattern
sees" is how the browse screen and the bulk editor would come to disagree about whether an
item is excluded, and the disagreement would be invisible until a batch wrote to something
the operator had filtered out. ``tests/unit/test_exclusion.py`` asserts the two agree.
"""

from __future__ import annotations

from typing import Protocol

from metaedit.adapters.jellyfin.dto import BaseItemDto
from metaedit.adapters.lastfm.canonical import normalize_tag


class ItemLabels(Protocol):
    """The slice of the API's item summary a pattern reads.

    Read-only properties, so the pydantic model satisfies it without the service layer
    importing it and without the domain learning about pydantic.
    """

    @property
    def name(self) -> str: ...
    @property
    def album(self) -> str | None: ...
    @property
    def album_artist(self) -> str | None: ...


def _labels(*values: str | None) -> tuple[str, ...]:
    """Non-empty labels, de-duplicated by normalised form, in the order given.

    De-duplicated rather than returned as-is because an item whose album and album artist
    are the same string (a self-titled record by an artist whose name matches) would
    otherwise be compared twice for every pattern, and the report would name the wrong
    occurrence of the same text.
    """
    labels: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not value:
            continue
        text = value.strip()
        key = normalize_tag(text)
        if not key or key in seen:
            continue
        seen.add(key)
        labels.append(text)
    return tuple(labels)


def labels_from_summary(summary: ItemLabels) -> tuple[str, ...]:
    """Labels for one API browse row."""
    return _labels(summary.name, summary.album, summary.album_artist)


def labels_from_dto(dto: BaseItemDto) -> tuple[str, ...]:
    """Labels for one Jellyfin item, from whichever spelling the server sent.

    ``album_artist_names`` is the DTO's own accessor for the three spellings of the album
    artist, so this reads the same field the write path does rather than a parallel guess.
    """
    return _labels(dto.Name, dto.Album, *dto.album_artist_names())
