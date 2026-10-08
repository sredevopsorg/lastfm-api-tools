"""A stub Jellyfin for end-to-end tests.

The end-to-end journey *writes*, so it must never point at a real library. This stands in
for the server: it implements the handful of endpoints the app uses, holds a small music
library, and records every write so a test can assert on the exact body that was sent.

It deliberately reproduces the behaviours that cost real debugging time against Jellyfin
12.2.0, so the app is exercised against the quirks rather than against a convenient
fiction:

* authentication is the ``MediaBrowser`` scheme; a bare ``Authorization: <key>`` is 401
* an API key is userless, so ``GET /Users/Me`` is **400**, not 200
* ``GET /Artists`` returns nothing if given ``includeItemTypes``
* ``GET /Library/MediaFolders`` has no ``ItemId``; ``/Library/VirtualFolders`` does
* items carry an ``Etag``, but only when it is requested via ``fields=``
* ``POST /Items/{id}`` is a **full overwrite**: any key the body omits becomes null
* an **unrecognised ``sortBy`` is silently ignored**, which is why the app validates sort
  keys against an allow-list rather than passing them through

That last one is the whole reason this tool exists, so the stub implements it faithfully
rather than merging.

Paging is real: ``TotalRecordCount`` is the size of the matched set *before* the window is
applied, not the size of the window. Getting that wrong is not a detail -- it is exactly
the bug the browse rework fixed, where a header reported the total while the table held one
page of it, so a stub that reported the window size would make that bug invisible.
"""

from __future__ import annotations

import json
import os
import re
import sys
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

# The writable field set, mirroring metaedit.domain.writable. Duplicated here on purpose:
# importing the app's own definition would make the stub agree with the app by
# construction, so a bug in that definition would be invisible to these tests.
WRITABLE_FIELDS = (
    "Name",
    "ForcedSortName",
    "OriginalTitle",
    "Overview",
    "Genres",
    "Tags",
    "Studios",
    "ProductionLocations",
    "ProviderIds",
    "ExternalUrls",
    "CommunityRating",
    "CriticRating",
    "PremiereDate",
    "ProductionYear",
    "OfficialRating",
    "CustomRating",
    "PreferredMetadataLanguage",
    "PreferredMetadataCountryCode",
    "People",
    "LockData",
    "LockedFields",
)

API_KEY = os.environ.get("STUB_API_KEY", "stub-admin-key")

# Item id shapes Jellyfin will parse as a filter value. Duplicated from the app rather than
# imported, for the same reason the writable field list is: a stub that shares the app's
# own definition agrees with it by construction, so a bug in the definition is invisible.
_FACET_ID_PATTERN = re.compile(
    r"\A(?:[0-9a-fA-F]{32}|"
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\Z"
)


def _credited_artist_ids(row: dict[str, Any]) -> set[str]:
    """Every artist id an item is credited to, from whichever spelling it carries.

    Live-verified that `ArtistIds` and `AlbumArtistIds` return the *same* counts for the
    same id (21 songs for ABBA either way), so one reader for both lists is faithful rather
    than a shortcut. `ContributingArtistIds` returns 0 and is deliberately absent.
    """
    ids: set[str] = set()
    for field_name in ("ArtistItems", "AlbumArtists"):
        for pair in row.get(field_name) or []:
            if isinstance(pair, dict) and pair.get("Id"):
                ids.add(str(pair["Id"]))
    return ids


# Enough extra artists that the default page size (50) leaves a second, partial page. A
# library smaller than one page cannot exercise paging at all, and a library of exactly
# one page cannot exercise a *partial* last page, which is where the paging arithmetic in
# the frontend went wrong.
PAGE_FILLER_COUNT = 58

app = FastAPI(title="stub-jellyfin")

# ---------------------------------------------------------------------- state


def _artist(
    item_id: str, name: str, mbid: str | None, genres: list[str] | None = None, overview: str = ""
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "Id": item_id,
        "Type": "MusicArtist",
        "Name": name,
        "Etag": f"etag-{item_id}-1",
        "SourceType": "Library",
        "Genres": genres or [],
        "Tags": ["keep-me"],
        "Studios": [],
        "ProductionLocations": [],
        "ProviderIds": {"MusicBrainzArtist": mbid} if mbid else {},
        "ExternalUrls": [],
        "LockedFields": [],
        "People": [],
        "Overview": overview,
        "LockData": False,
    }
    return item


def _album(
    item_id: str,
    name: str,
    album_artist: str,
    mbid: str | None,
    artist_id: str | None = None,
    genres: list[str] | None = None,
) -> dict[str, Any]:
    """An album as the server sends it: scalar `AlbumArtist`, no `AlbumArtists` array.

    Reproducing that spelling matters -- reading only the array left an album with no
    artist, which made the Last.fm lookup impossible to derive.

    ``AlbumArtists``/``ArtistItems`` are alongside it, not instead of it, because that is
    what the live server sends when `fields=` asks and it is what `ArtistIds` matches
    against. An album with no credit pair is unfilterable by artist, and the filter would
    look broken while behaving correctly.
    """
    credits = [{"Name": album_artist, "Id": artist_id}] if artist_id else []
    return {
        "Id": item_id,
        "Type": "MusicAlbum",
        "Name": name,
        "Etag": f"etag-{item_id}-1",
        "SourceType": "Library",
        "AlbumArtist": album_artist,
        "Artists": [album_artist],
        "AlbumArtists": credits,
        "ArtistItems": credits,
        "Genres": genres or [],
        "Tags": ["keep-me"],
        "Studios": [],
        "ProductionLocations": [],
        "ProviderIds": {"MusicBrainzAlbum": mbid} if mbid else {},
        "ExternalUrls": [],
        "LockedFields": [],
        "People": [],
        "Overview": "",
        "LockData": False,
    }


def _song(
    item_id: str,
    name: str,
    album: str,
    album_id: str,
    artist: str,
    artist_id: str,
    genres: list[str] | None = None,
) -> dict[str, Any]:
    """A song, which is the only media type both facets can narrow.

    `Album`/`AlbumId` are what an exclusion pattern reads and what `AlbumIds` matches; the
    credit pair is what `ArtistIds` matches. `AlbumArtists` and `ArtistItems` deliberately
    differ here for the compilation case -- `Various Artists` is the album artist and the
    track artist is someone else -- because that divergence is the whole reason the filter
    reads both lists.
    """
    return {
        "Id": item_id,
        "Type": "Audio",
        "Name": name,
        "Etag": f"etag-{item_id}-1",
        "SourceType": "Library",
        "Album": album,
        "AlbumId": album_id,
        "AlbumArtist": artist,
        "Artists": [artist],
        "AlbumArtists": [{"Name": artist, "Id": artist_id}],
        "ArtistItems": [{"Name": artist, "Id": artist_id}],
        "Genres": genres or [],
        "Tags": ["keep-me"],
        "Studios": [],
        "ProductionLocations": [],
        "ProviderIds": {},
        "ExternalUrls": [],
        "LockedFields": [],
        "People": [],
        "Overview": "",
        "LockData": False,
    }


# One id space, and it is the 32-hex one.
#
# Two things have to agree and a fixture can easily let them differ: the item's `Id`, which
# is the key `GET /Items?ids=` resolves, and the id inside its credit pairs, which is what
# `ArtistIds` matches. On a real server they are the same string. They were not here at
# first -- the keys were `art-1` and the credit pairs carried a hex id -- so the facet picker
# stored an id that resolved to nothing and every chip rendered as a raw id slice while the
# filter itself worked. A fixture that lets the two differ cannot catch that class of bug;
# it manufactures it.


# The item's own `Id` and the id a filter is given are the same string, as they are on a
# real server, and that is not a detail -- it is the difference between a fixture that
# catches a broken picker and one that manufactures the bug.
#
# It has to be the 32-hex shape, because Jellyfin parses only that (or a dashed GUID) as a
# filter value; a filter naming `art-1` is discarded *in full* and returns the whole library.
# So the ids are hex, and the readable names are library *keys* only -- which is what keeps
# the existing specs, which address items as `art-1` through the stub's own `/__writes`
# endpoint, working unchanged.
def _hex_id(prefix: str, index: int) -> str:
    """A 32-hex id that is distinct per index.

    Zero-padded to a fixed width, because the obvious `prefix * 31 + str(index)` produces a
    33-character id at index 10 -- caught by asserting every fixture id matches the shape
    Jellyfin parses, which is the same assertion the app's own guard makes.
    """
    return prefix * (32 - len(str(index))) + str(index)


ARTIST_IDS = {
    "art-1": _hex_id("a", 1),
    "art-2": _hex_id("a", 2),
    "art-3": _hex_id("a", 3),
}
ALBUM_IDS = {
    "alb-1": _hex_id("b", 1),
    "alb-2": _hex_id("b", 2),
    "alb-3": _hex_id("b", 3),
}

# Keyed by the *server id*, because that is what every Jellyfin endpoint addresses:
# `GET /Items?ids=`, `GET /Items/{id}`, `POST /Items/{id}`. Keeping the readable names as
# keys instead made `LIBRARY.get(hex_id)` return None, so the editor 404'd for every item
# the browse screen linked to -- a fixture that worked for the list endpoints and not the
# single-item ones, which is the half of the API nothing would have exercised.
_BY_KEY: dict[str, dict[str, Any]] = {
    "alb-1": _album(
        ALBUM_IDS["alb-1"],
        "OK Computer",
        "Radiohead",
        "b1392450-e666-3926-a536-22c65f834433",
        ARTIST_IDS["art-1"],
    ),
    "alb-2": _album(ALBUM_IDS["alb-2"], "Dummy", "Portishead", None, ARTIST_IDS["art-2"]),
    "alb-3": _album(ALBUM_IDS["alb-3"], "Live at Leeds", "Radiohead", None, ARTIST_IDS["art-1"]),
    "art-1": _artist(
        ARTIST_IDS["art-1"],
        "Radiohead",
        "a74b1b7f-71a5-4011-9441-d0b5e4122711",
        genres=["Rock"],
        overview="",
    ),
    "art-2": _artist(
        ARTIST_IDS["art-2"],
        "Portishead",
        "8f6bd1e4-fbe1-4f50-aa9b-94c450ec0f11",
        genres=[],
    ),
    "art-3": _artist(ARTIST_IDS["art-3"], "Nobody At All", None),
}

# Songs, so `AlbumIds` has something to narrow -- it is the one facet that applies to songs
# only. Two on one album and one on another, which is what makes union-within-a-parameter
# and intersection-across-parameters distinguishable: with a single song per album a filter
# that ignored either one would still return a plausible number.
for index, (name, album_key, artist_key) in enumerate(
    [
        ("Airbag", "alb-1", "art-1"),
        ("Paranoid Android", "alb-1", "art-1"),
        ("Roads", "alb-2", "art-2"),
    ],
    start=1,
):
    _BY_KEY[f"song-{index}"] = _song(
        _hex_id("c", index),
        name,
        _BY_KEY[album_key]["Name"],
        ALBUM_IDS[album_key],
        _BY_KEY[artist_key]["Name"],
        ARTIST_IDS[artist_key],
        genres=["Rock"] if index == 1 else None,
    )

# A page's worth of extra artists, so paging has a second page and an operator can select
# across the boundary. Named so their order by name is obvious, and every third one has no
# genres so the missing filter has something to find.
for index in range(1, PAGE_FILLER_COUNT + 1):
    item_id = f"art-fill-{index:03d}"
    _BY_KEY[item_id] = _artist(
        _hex_id("d", index),
        f"Filler Artist {index:03d}",
        f"mbid-filler-{index:03d}",
        genres=[] if index % 3 == 0 else ["Ambient"],
        overview=f"An overview for filler {index}." if index % 2 == 0 else "",
    )

# Two rows with the SAME name. `ORDER BY name` cannot separate them, which is the condition
# that broke archive paging on the live data -- and any ordering the app applies to a paged
# list has to survive it.
_BY_KEY["art-dupe-a"] = _artist(_hex_id("e", 1), "Twin Peaks", "mbid-dupe-a", genres=["Shoegaze"])
_BY_KEY["art-dupe-b"] = _artist(_hex_id("e", 2), "Twin Peaks", "mbid-dupe-b", genres=["Krautrock"])

# The library as every endpoint sees it, plus the readable names kept only for the test
# hooks and the seed's own cross-references.
LIBRARY: dict[str, dict[str, Any]] = {str(row["Id"]): row for row in _BY_KEY.values()}
LIBRARY_KEYS: dict[str, str] = {key: str(row["Id"]) for key, row in _BY_KEY.items()}

WRITES: list[dict[str, Any]] = []
ETAG_COUNTER = {"n": 1}

# A pristine copy, so each test starts from the same library. Restoring only the write
# log was not enough: values applied by one test leak into the next, a field then reads
# as already-correct, and its checkbox is disabled.
PRISTINE: dict[str, dict[str, Any]] = {}


# ---------------------------------------------------------------- auth checks


def _authorised(request: Request) -> bool:
    """Only the MediaBrowser scheme is accepted, as on a real 12.x server."""
    header = request.headers.get("authorization", "")
    if not header.startswith("MediaBrowser "):
        return False
    return f'Token="{API_KEY}"' in header


@app.middleware("http")
async def require_auth(request: Request, call_next: Any) -> Response:
    path = request.url.path
    public = path in ("/System/Info/Public", "/__writes", "/__reset", "/health")
    if public:
        return await call_next(request)
    if not _authorised(request):
        return JSONResponse({"title": "Unauthorized"}, status_code=401)
    return await call_next(request)


# ----------------------------------------------------------------- test hooks


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/__writes")
async def writes() -> dict[str, Any]:
    """What the app sent, so a test can assert on the exact body.

    ``items`` is keyed by server id -- it mirrors the library -- and ``ids`` is the readable
    key for each of them. Both are exposed because a spec needs each for a different job:
    asserting on a write needs the id the app actually sent, and starting a test needs to
    say *which* item it means without hard-coding a hex string. Keying the library by its
    readable keys instead was the earlier shape, and it silently made `art-1` a value that
    no facet filter would accept.
    """
    return {
        "writes": WRITES,
        "items": LIBRARY,
        "ids": LIBRARY_KEYS,
    }


@app.post("/__reset")
async def reset() -> dict[str, str]:
    """Restore the library to its seeded content and forget every write."""
    import copy

    if not PRISTINE:
        PRISTINE.update(copy.deepcopy(LIBRARY))
    WRITES.clear()
    ETAG_COUNTER["n"] = 1
    for item_id, original in PRISTINE.items():
        LIBRARY[item_id] = copy.deepcopy(original)
    return {"status": "reset"}


# -------------------------------------------------------------------- Jellyfin


@app.get("/System/Info/Public")
async def system_info_public() -> dict[str, Any]:
    return {"ServerName": "stub", "Version": "12.2.0", "Id": "stub-server"}


@app.get("/System/Info")
async def system_info() -> dict[str, Any]:
    return {"Version": "12.2.0", "ServerName": "stub"}


@app.get("/Users/Me")
async def users_me(request: Request) -> JSONResponse:
    """An API key is userless, so a real server answers 400 here.

    Reproduced deliberately: treating this as an error is the bug that made every
    API-key deployment look unable to write.
    """
    if request.headers.get("x-stub-user-token") == "yes":
        return JSONResponse({"Id": "u1", "Name": "admin", "Policy": {"IsAdministrator": True}})
    return JSONResponse({"title": "Bad Request", "status": 400}, status_code=400)


@app.get("/Users")
async def users() -> list[dict[str, Any]]:
    """A user list, because a userless API key needs one to read a single item."""
    return [{"Id": "e2e-user", "Name": "e2e", "Policy": {"IsAdministrator": True}}]


@app.get("/Library/VirtualFolders")
async def virtual_folders() -> list[dict[str, Any]]:
    return [
        {
            "Name": "Music",
            "ItemId": "lib-music",
            "CollectionType": "music",
            "Locations": ["/media/Music"],
        }
    ]


@app.get("/Library/MediaFolders")
async def media_folders() -> dict[str, Any]:
    # No ItemId, exactly as a real server returns it.
    return {"Items": [{"Name": "Music", "ItemId": None, "CollectionType": "music"}]}


@app.get("/Genres")
async def genres(request: Request) -> dict[str, Any]:
    """The genre entities for one media type.

    Live-verified on 12.2.0 that this is the list the server's own genre filter offers (39
    entries for the real library's artists), which is why the removal screen reads it
    rather than scanning items for a vocabulary -- a scan would be a second opinion that
    could disagree with the filter the operator actually sees.

    Deduplicated and sorted by name, as the server returns them.
    """
    wanted = (request.query_params.get("includeItemTypes") or "").strip()
    names: set[str] = set()
    for row in LIBRARY.values():
        if wanted and row.get("Type") != wanted:
            continue
        for genre in row.get("Genres") or []:
            if genre:
                names.add(str(genre))
    ordered = sorted(names)
    return {
        "Items": [{"Name": name, "Id": name} for name in ordered],
        "TotalRecordCount": len(ordered),
    }


@app.get("/Artists")
async def artists(request: Request) -> dict[str, Any]:
    """Kept even though nothing calls it: it is the endpoint whose `includeItemTypes`
    behaviour was the trap, and a future change that reaches for it should meet the real
    behaviour rather than a convenient one. The browse itself uses `/Items`."""
    # `includeItemTypes` empties the result on a real server.
    if request.query_params.get("includeItemTypes"):
        return {"Items": [], "TotalRecordCount": 0}
    term = (request.query_params.get("searchTerm") or "").lower()
    if term and "radiohead" not in term:
        return {"Items": [], "TotalRecordCount": 0}
    items = list(LIBRARY.values())
    return {"Items": items, "TotalRecordCount": len(items)}


# Jellyfin's sort keys, as the live server implements them. `SortName` honours
# ForcedSortName, which is the whole reason the app offers it separately from `Name`.
SORT_KEYS: dict[str, Any] = {
    "Name": lambda row: str(row.get("Name") or "").casefold(),
    "SortName": lambda row: str(row.get("ForcedSortName") or row.get("Name") or "").casefold(),
    "DateCreated": lambda row: str(row.get("DateCreated") or ""),
    "ProductionYear": lambda row: row.get("ProductionYear") or 0,
}


@app.get("/Items")
async def items(request: Request) -> dict[str, Any]:
    raw_ids = request.query_params.get("ids")
    if raw_ids:
        found = [LIBRARY[i] for i in raw_ids.split(",") if i in LIBRARY]
        return {"Items": found, "TotalRecordCount": len(found)}

    found = list(LIBRARY.values())
    wanted = (request.query_params.get("includeItemTypes") or "").strip()
    if wanted:
        types = {name for name in wanted.split(",") if name}
        found = [row for row in found if row.get("Type") in types]
    term = (request.query_params.get("searchTerm") or "").lower()
    if term:
        found = [row for row in found if term in str(row.get("Name", "")).lower()]

    # Jellyfin's own filters. `hasOverview=false` genuinely narrows on a real server
    # (641 -> 325 artists, measured). `Filters=IsMissing` does not, and is deliberately
    # absent here so the app cannot come to rely on it quietly working.
    has_overview = request.query_params.get("hasOverview")
    if has_overview in ("true", "false"):
        wanted_overview = has_overview == "true"
        found = [
            row for row in found if bool(str(row.get("Overview") or "").strip()) is wanted_overview
        ]
    years = request.query_params.get("Years")
    if years:
        wanted_years = {part for part in years.split(",") if part}
        found = [row for row in found if str(row.get("ProductionYear")) in wanted_years]

    # `Genres`/`Tags`: an exact, case-insensitive, server-side filter.
    #
    # Live-verified on 12.2.0 that each parameter matches a whole stored value and folds
    # case: `Genres=alternative rock` returns 23 artists, `Genres=ALTERNATIVE ROCK` the
    # same 23, and every returned item genuinely carries that value -- `Genres=rock`
    # returns 123 and none of them merely contain "rock". Modelling that faithfully is the
    # point of this block: a stub that ignored the parameter would make every removal test
    # pass while the real filter selected nothing, and a stub that applied substring
    # matching would make the exact-match guarantee look broken.
    #
    # The two are queried independently and *anded*, matching the server: a request naming
    # both asks for items carrying the value in both places.
    for field in ("Genres", "Tags"):
        wanted = request.query_params.get(field)
        if not wanted:
            continue
        found = [
            row
            for row in found
            if any(str(value).casefold() == wanted.casefold() for value in (row.get(field) or []))
        ]

    # `ArtistIds`/`AlbumIds`: union within a parameter, intersect across them, and -- the
    # part worth modelling -- an unparseable list is discarded *in full*. Live-verified:
    # `ArtistIds=abc`, a pipe-joined pair and a 40-character string each returned the entire
    # library, while a junk entry alongside a valid one is dropped individually. A stub that
    # quietly ignored a bad list would make the app's id validation look like needless
    # defensiveness, and the app would be free to remove it.
    for name, reader in (
        ("ArtistIds", _credited_artist_ids),
        (
            "AlbumIds",
            lambda row: {str(row["AlbumId"])} if row.get("AlbumId") else set(),
        ),
    ):
        raw = request.query_params.get(name)
        if raw is None:
            continue
        wanted_ids = {
            entry.strip() for entry in raw.split(",") if _FACET_ID_PATTERN.match(entry.strip())
        }
        if not wanted_ids:
            continue
        found = [row for row in found if reader(row) & wanted_ids]

    # Sort BEFORE the window. Sorting after slicing would sort one page -- which is the
    # mistake the frontend made by sorting in the browser, and a stub that did it here
    # would make that mistake look correct.
    sort_by = (request.query_params.get("sortBy") or "SortName").split(",")[0]
    key = SORT_KEYS.get(sort_by)
    # An unrecognised sortBy is IGNORED, not rejected -- live-verified on 12.2.0. This is
    # why the app validates against an allow-list: from here, a wrong sort is
    # indistinguishable from a right one.
    if key is not None:
        reverse = request.query_params.get("sortOrder") == "Descending"
        found = sorted(found, key=key, reverse=reverse)

    # The matched-set size, BEFORE the window. Reporting the window size instead is not a
    # detail -- it is the bug the browse rework fixed, and a stub that got it wrong would
    # make a header claiming "50 total" for a 64-item library look correct.
    total = len(found)

    offset = request.query_params.get("startIndex")
    if offset and str(offset).isdigit():
        found = found[int(offset) :]
    limit = request.query_params.get("limit")
    if limit and str(limit).isdigit():
        found = found[: int(limit)]

    # `Etag` only when asked for, matching the single-item endpoint.
    fields = request.query_params.get("fields") or ""
    payload = [dict(row) for row in found]
    if "Etag" not in fields:
        for row in payload:
            row.pop("Etag", None)
    return {"Items": payload, "TotalRecordCount": total}


@app.get("/Items/{item_id}")
async def item(item_id: str, request: Request) -> JSONResponse:
    # Reproduces a live 12.2.0 behaviour: with a userless API key this endpoint returns
    # **400** for every id unless a `userId` is supplied, even for an id the server itself
    # just returned from /Artists. The list endpoints tolerate its absence. Without this
    # the stub accepted a request the real server rejects, so the whole single-item path
    # -- and therefore apply -- shipped broken.
    if not request.query_params.get("userId"):
        return JSONResponse({"title": "Bad Request"}, status_code=400)
    row = LIBRARY.get(item_id)
    if row is None:
        return JSONResponse({"title": "Not Found"}, status_code=404)
    payload = dict(row)
    # An Etag is only returned when it is asked for, as on a real server.
    fields = request.query_params.get("fields") or ""
    if "Etag" not in fields:
        payload.pop("Etag", None)
    return JSONResponse(payload)


@app.post("/Items/{item_id}")
async def update_item(item_id: str, request: Request) -> Response:
    """A full overwrite: every writable key absent from the body becomes null.

    This is the hazard the app is built around, so the stub performs it rather than
    merging. A test that only checked the body it sent would pass even if the app had
    omitted a field, because the damage happens here.
    """
    if item_id not in LIBRARY:
        return JSONResponse({"title": "Not Found"}, status_code=404)
    body = json.loads(await request.body() or b"{}")
    WRITES.append({"item_id": item_id, "body": body})

    current = LIBRARY[item_id]
    for field in WRITABLE_FIELDS:
        current[field] = body.get(field)  # absent -> null, exactly like the real thing
    for key, value in body.items():
        current[key] = value
    ETAG_COUNTER["n"] += 1
    current["Etag"] = f"etag-{item_id}-{ETAG_COUNTER['n']}"
    return Response(status_code=204)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app, host="0.0.0.0", port=int(os.environ.get("STUB_PORT", "8096")), log_level="warning"
    )
    sys.exit(0)
