"""Fetching Last.fm data for Jellyfin items and storing it in the archive.

This is the step that fills the archive from the UI. Until now the archive could only be
populated by the live test suite, so a fresh deployment had nothing to propose and the
editor had no candidate to show -- the flow was unclosable.

Three decisions shape it:

* **The query is derived from the Jellyfin item, never guessed.** An album's artist and
  title, a track's artist and title, an artist's name. Where Jellyfin already holds a
  MusicBrainz id, that id is used instead of a name, because an id cannot autocorrect to
  the wrong entity.
* **A miss is a normal outcome, not an error.** Last.fm answers "not found" for plenty of
  real library entries, and the response *is* archived so the absence is dated. A miss
  additionally offers search alternatives, so the operator can pick the right entity
  rather than being told only that the guess failed.
* **Every response is archived through the normal store**, so the derived layer, the
  storage cap and the provenance trail all behave exactly as they do for a live fetch.
  There is no separate "import" path that could drift from the fetch path.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from metaedit.adapters.lastfm.client import LastfmClient
from metaedit.domain.errors import ArchiveCapReached, LastfmError, LastfmNotFound
from metaedit.domain.snapshot import NormalizedItem
from metaedit.domain.writable import ItemKind
from metaedit.logging import get_logger

log = get_logger(__name__)


@dataclass(slots=True)
class SearchAlternative:
    """A Last.fm search hit, offered when the derived query missed."""

    name: str
    artist: str | None = None
    url: str | None = None
    listeners: int | None = None


@dataclass(slots=True)
class HarvestOutcome:
    """What happened for one item, in enough detail to show and to act on."""

    item_id: str
    name: str
    kind: ItemKind
    derived_query: dict[str, str | None] = field(default_factory=dict)
    found: bool = False
    # Last.fm methods that returned data, in the order they were asked.
    methods: list[str] = field(default_factory=list)
    # Content ids of the stored bodies, so a written value traces back to its response.
    response_ids: list[str] = field(default_factory=list)
    # How many calls were served from the archive rather than the network: the number
    # the archive is saving, and what makes a re-run cheap.
    from_archive: int = 0
    error: str | None = None
    error_code: str | None = None
    alternatives: list[SearchAlternative] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "name": self.name,
            "kind": self.kind,
            "derived_query": self.derived_query,
            "found": self.found,
            "methods": self.methods,
            "response_ids": self.response_ids,
            "from_archive": self.from_archive,
            "error": self.error,
            "error_code": self.error_code,
            "alternatives": [
                {
                    "name": alt.name,
                    "artist": alt.artist,
                    "url": alt.url,
                    "listeners": alt.listeners,
                }
                for alt in self.alternatives
            ],
        }


def derive_query(item: NormalizedItem) -> dict[str, str | None]:
    """The Last.fm query for a Jellyfin item.

    A MusicBrainz id is preferred where the item has one: ``autocorrect`` can only
    correct a *name*, and correcting is exactly what we do not want when the id is
    already unambiguous.
    """
    provider_ids = item.get("ProviderIds") or {}
    title = (item.name or "").strip()
    artist = None
    for candidate in (*(item.artist_names or []), *(item.album_artist_names or [])):
        if isinstance(candidate, str) and candidate.strip():
            artist = candidate.strip()
            break
    match item.kind:
        case "MusicArtist":
            return {
                "artist": title or None,
                "mbid": provider_ids.get("MusicBrainzArtist"),
            }
        case "MusicAlbum":
            return {
                "artist": artist,
                "album": title or None,
                "mbid": provider_ids.get("MusicBrainzAlbum"),
            }
        case _:
            return {
                "artist": artist,
                "track": title or None,
                "mbid": provider_ids.get("MusicBrainzTrack"),
            }


async def harvest_item(
    *,
    session: AsyncSession,
    client: LastfmClient,
    item: NormalizedItem,
    search_fallback: bool = True,
) -> HarvestOutcome:
    """Fetch everything we need for one item and archive it.

    Never raises for a Last.fm-side problem: an item that cannot be found is a reported
    outcome, because a library-wide fetch would otherwise abort on the first obscure
    track. Auth and storage-cap failures *do* propagate -- those are conditions the
    operator has to fix, and continuing would burn rate limit for nothing.
    """
    query = derive_query(item)
    outcome = HarvestOutcome(
        item_id=str(item.item_id),
        name=item.name or "",
        kind=item.kind,
        derived_query=query,
    )

    missing = [
        key
        for key, value in query.items()
        if key != "mbid" and (value is None or not str(value).strip())
    ]
    if not query.get("mbid") and missing:
        outcome.error = (
            f"Jellyfin holds no {', '.join(missing)} for this item, so there is nothing "
            "to look up. Fill those in first, or search Last.fm directly."
        )
        outcome.error_code = "insufficient_item_data"
        return outcome

    calls = harvest_calls(client, item.kind, query)
    saw_not_found = False
    for method, call in calls:
        try:
            # Every client method returns (parsed model, LastfmResult): the model is what
            # the caller wanted, the result carries archive provenance.
            _model, result = await call()
        except LastfmNotFound:
            # Archived by the client, so the absence is dated; keep going, because a
            # missing tag list should not discard an otherwise good getInfo.
            saw_not_found = True
            continue
        except LastfmError as exc:
            outcome.error = exc.message
            outcome.error_code = exc.code
            break

        outcome.methods.append(method)
        if result.response_id:
            outcome.response_ids.append(result.response_id)
        if result.served_from_archive:
            outcome.from_archive += 1

    outcome.found = bool(outcome.methods)

    if not outcome.found and search_fallback:
        outcome.alternatives = await _search(client, item.kind, query)
        if not outcome.error:
            outcome.error = (
                "Last.fm has no match for this item."
                if saw_not_found
                else "The Last.fm lookup returned nothing."
            )
            outcome.error_code = "not_found"

    return outcome


async def harvest_items(
    *,
    session: AsyncSession,
    client: LastfmClient,
    items: Sequence[NormalizedItem],
    search_fallback: bool = True,
) -> AsyncIterator[HarvestOutcome]:
    """Harvest a batch, yielding each outcome as it completes.

    The caller is expected to rebuild the derived layer **once** after draining this
    generator, not per item: reindexing is a pass over the whole raw layer, so doing it
    inside the loop would make a batch quadratic for no benefit. Items therefore stream
    as they arrive, and the archive becomes consistent when the batch ends.
    """
    total = len(items)
    for index, item in enumerate(items):
        try:
            outcome = await harvest_item(
                session=session, client=client, item=item, search_fallback=search_fallback
            )
        except ArchiveCapReached:
            # Fatal for the batch: every remaining fetch would be refused too, and each
            # attempt would spend rate limit.
            log.warning("harvest_cap_reached", item_id=item.item_id, total=total)
            raise

        yield outcome
        log.info(
            "harvest_item",
            index=index,
            total=total,
            item_id=item.item_id,
            found=outcome.found,
            methods=outcome.methods,
        )


async def _search(
    client: LastfmClient, kind: ItemKind, query: dict[str, str | None]
) -> list[SearchAlternative]:
    """Search Last.fm so a miss comes with something to choose from.

    An artist item searches artists; an album or track searches that kind, falling back
    to an artist search when the specific kind finds nothing, since the artist is
    usually still the thing worth harvesting.
    """
    try:
        artist = query.get("artist")
        if kind == "MusicArtist" and artist:
            found, _ = await client.artist_search(artist, limit=8)
        elif kind == "MusicAlbum":
            term = " ".join(part for part in (query.get("artist"), query.get("album")) if part)
            found, _ = await client.album_search(term, limit=8)
        else:
            term = " ".join(part for part in (query.get("artist"), query.get("track")) if part)
            found, _ = await client.album_search(term, limit=8)
    except LastfmError:
        return []
    return [
        SearchAlternative(name=hit.name, artist=hit.artist, url=hit.url, listeners=hit.listeners)
        for hit in found
        if hit.name
    ]


def harvest_calls(
    client: LastfmClient, kind: ItemKind, query: dict[str, str | None]
) -> list[tuple[str, Callable[[], Awaitable[tuple[Any, Any]]]]]:
    """The concrete client calls for an item, bound to this client."""
    mbid = query.get("mbid")
    artist = query.get("artist")
    if kind == "MusicArtist":
        return [
            ("artist.getinfo", lambda: client.artist_info(artist=artist, mbid=mbid)),
            ("artist.gettoptags", lambda: client.artist_top_tags(artist=artist, mbid=mbid)),
            ("artist.getsimilar", lambda: client.artist_similar(artist=artist, mbid=mbid)),
        ]
    if kind == "MusicAlbum":
        album = query.get("album")
        return [
            ("album.getinfo", lambda: client.album_info(artist=artist, album=album, mbid=mbid)),
            (
                "album.gettoptags",
                lambda: client.album_top_tags(artist=artist, album=album, mbid=mbid),
            ),
        ]
    del kind
    track = query.get("track")
    return [
        ("track.getinfo", lambda: client.track_info(artist=artist, track=track, mbid=mbid)),
        (
            "track.gettoptags",
            lambda: client.track_top_tags(artist=artist, track=track, mbid=mbid),
        ),
    ]
