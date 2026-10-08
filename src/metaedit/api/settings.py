"""Operator settings, edited at runtime.

The blacklist lives here rather than in the environment because it is *policy*, not
deployment configuration: it changes as an operator learns what a community tag set
contains, and requiring a container restart for that made the feature effectively
unusable. The env var is still honoured and still merges in, so an existing deployment
keeps working unchanged.

Two things this endpoint deliberately does *not* do:

* It never writes to Jellyfin. Blacklisting a genre changes what a future plan proposes.
  Removing a genre from a library is a separate, reviewed, revertible operation
  (``/api/bulk/remove-genre/*``), because "stop proposing this" and "delete this from 500
  items" have very different blast radii and the second must not be a side effect of the
  first.
* It does not reject a block of input because one line is ambiguous. The unambiguous
  lines are saved and the ambiguous ones are returned with both readings, so the operator
  can answer rather than start over.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from metaedit.adapters.jellyfin.client import JellyfinClient
from metaedit.api.deps import JellyfinDep
from metaedit.config import Settings, get_settings
from metaedit.db.session import get_session
from metaedit.domain.genre_blacklist import (
    Conflict,
    parse,
)
from metaedit.domain.genre_blacklist import (
    conflicts as find_conflicts,
)
from metaedit.domain.writable import ItemKind
from metaedit.service import genre_blacklist as service

router = APIRouter(prefix="/settings", tags=["settings"])

# Which media type to read the live genre vocabulary from when previewing. Artists are
# the cheapest and cover the widest vocabulary; the preview is advisory, so one kind is
# enough and asking for all three would triple the request count for a hint.
PREVIEW_KIND: ItemKind = "MusicArtist"
_PREVIEW_LIMIT = 500


class BlacklistConflictView(BaseModel):
    """A line that could be read two ways."""

    raw: str
    whole: str
    fragments: list[str]
    message: str


class GenreBlacklistResponse(BaseModel):
    """The blacklist, with each source named.

    Separately *and* merged. Separately, because an operator who saved an entry and did
    not see it take effect needs to know which source supplied what; merged, because
    ``effective`` is what the tag policy actually enforces.
    """

    entries: list[str]
    defaults: list[str]
    env: list[str]
    effective: list[str]
    counts: dict[str, int]
    # What an entry means, so the UI never has to explain the rule itself.
    note: str


class GenreBlacklistUpdate(BaseModel):
    raw: str = Field(
        default="",
        description="one genre per line. A line containing a comma is reported rather "
        "than split, because a comma is also a legal character in a genre value.",
    )
    allow_commas: bool = Field(
        default=False,
        description="split lines on commas. Only set this once the operator has been told "
        "what a comma would mean; on a library whose genres contain commas it changes "
        "which values are blacklisted.",
    )


class UpdateResponse(BaseModel):
    saved: list[str]
    count: int
    conflicts: list[BlacklistConflictView]
    state: GenreBlacklistResponse
    # True when something needs the operator's attention before the save means what they
    # may have intended. Not an error: the unambiguous entries were still saved.
    needs_review: bool


class PreviewEntry(BaseModel):
    entry: str
    norm: str
    # Live genre values this entry would match, as the library spells them.
    matches: list[str]


class PreviewResponse(BaseModel):
    preview: list[PreviewEntry]
    would_blacklist: list[str]
    conflicts: list[BlacklistConflictView]
    vocabulary_size: int
    scanned_kind: str
    # Why the vocabulary is what it is, so a small number is not read as a bug.
    note: str


BLACKLIST_NOTE = (
    "Matching is exact and case-insensitive: 'Rock' blocks 'rock' and 'ROCK' but not "
    "'Gothic Rock' or 'Rockabilly'. Entries are matched against the values that would be "
    "written to Genres, which are the individual pieces of a multi-valued tag -- so "
    "blacklisting 'rock' also blocks the 'Rock' piece of 'Rock, Reggae'."
)


def _state_view(state: service.BlacklistState) -> GenreBlacklistResponse:
    return GenreBlacklistResponse(
        entries=sorted(state.stored),
        defaults=sorted(state.defaults),
        env=sorted(state.env),
        effective=sorted(state.effective),
        counts=state.counts,
        note=BLACKLIST_NOTE,
    )


def _conflict_view(conflict: Conflict) -> BlacklistConflictView:
    return BlacklistConflictView(
        raw=conflict.raw,
        whole=conflict.whole,
        fragments=conflict.fragments,
        message=conflict.message,
    )


@router.get("/genre-blacklist", response_model=GenreBlacklistResponse)
async def read_genre_blacklist(
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> GenreBlacklistResponse:
    """The current blacklist and the three sources it is drawn from."""
    return _state_view(await service.load(session, settings))


@router.put("/genre-blacklist", response_model=UpdateResponse)
async def write_genre_blacklist(
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    body: Annotated[GenreBlacklistUpdate, Body()],
) -> UpdateResponse:
    """Replace the stored list from text.

    Replace rather than merge: the input is a textarea, so what the operator sees when
    they press save is what they mean, and a hidden union with previous entries would make
    deleting one impossible.

    A 200 with ``needs_review`` is the normal outcome for ambiguous input, not a 422.
    Refusing the whole block would discard the entries that were unambiguous, and the
    operator would have to work out which line was the problem from an error alone.
    """
    result, conflicts = await service.replace(session, raw=body.raw, allow_commas=body.allow_commas)
    state = await service.load(session, settings)
    return UpdateResponse(
        saved=result.values,
        count=len(result.entries),
        conflicts=[_conflict_view(conflict) for conflict in conflicts],
        state=_state_view(state),
        needs_review=not result.clean,
    )


@router.post("/genre-blacklist/preview", response_model=PreviewResponse)
async def preview_genre_blacklist(
    client: JellyfinDep,
    session: Annotated[AsyncSession, Depends(get_session)],
    body: Annotated[GenreBlacklistUpdate, Body()],
) -> PreviewResponse:
    """What this text would blacklist, and which live genres it currently matches.

    Reads the library's genre vocabulary so the answer is in the operator's own words.
    This is what makes the comma ambiguity safe to offer: they see the exact values a
    line would match *before* saving, rather than discovering it when a genre they wanted
    is missing from a diff.

    Reads only. The vocabulary is sampled from one media type rather than all three,
    which is a deliberate approximation -- a genre on an album but no artist would not
    be listed -- and the response says so rather than implying completeness.
    """
    known = await _genre_vocabulary(client)
    parsed = parse(body.raw, allow_commas=body.allow_commas)
    # Conflicts are computed over the saved entries *and* the refused lines, so a line
    # that could not be saved is still shown to the operator with its readings.
    reported = find_conflicts(
        [entry.value for entry in parsed.entries] + [c.raw for c in parsed.conflicts],
        known=known,
    )

    preview: list[PreviewEntry] = []
    for entry in parsed.entries:
        hits = sorted(value for value in known if value.casefold() == entry.norm)
        preview.append(PreviewEntry(entry=entry.value, norm=entry.norm, matches=hits))

    return PreviewResponse(
        preview=preview,
        would_blacklist=[entry.value for entry in parsed.entries],
        conflicts=[_conflict_view(conflict) for conflict in reported],
        vocabulary_size=len(known),
        scanned_kind=str(PREVIEW_KIND),
        note=(
            "Matches are the library's spelling of each genre. Exact matching means an "
            "entry matches only the values listed here, not every genre containing it."
        ),
    )


async def _genre_vocabulary(client: JellyfinClient) -> set[str]:
    """Distinct genre values currently in the library, one media type's worth."""
    result = await client.items(kind=PREVIEW_KIND, limit=_PREVIEW_LIMIT)
    values: set[str] = set()
    for dto in result.Items:
        for genre in dto.Genres or []:
            if genre and genre.strip():
                values.add(genre.strip())
    return values


__all__ = ["router"]
