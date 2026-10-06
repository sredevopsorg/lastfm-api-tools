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

That last one is the whole reason this tool exists, so the stub implements it faithfully
rather than merging.
"""

from __future__ import annotations

import json
import os
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


def _album(item_id: str, name: str, album_artist: str, mbid: str | None) -> dict[str, Any]:
    """An album as the server sends it: scalar `AlbumArtist`, no `AlbumArtists` array.

    Reproducing that spelling matters -- reading only the array left an album with no
    artist, which made the Last.fm lookup impossible to derive.
    """
    return {
        "Id": item_id,
        "Type": "MusicAlbum",
        "Name": name,
        "Etag": f"etag-{item_id}-1",
        "SourceType": "Library",
        "AlbumArtist": album_artist,
        "Artists": [album_artist],
        "Genres": [],
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


LIBRARY: dict[str, dict[str, Any]] = {
    "alb-1": _album("alb-1", "OK Computer", "Radiohead", "b1392450-e666-3926-a536-22c65f834433"),
    "alb-2": _album("alb-2", "Dummy", "Portishead", None),
    "art-1": _artist(
        "art-1",
        "Radiohead",
        "a74b1b7f-71a5-4011-9441-d0b5e4122711",
        genres=["Rock"],
        overview="",
    ),
    "art-2": _artist(
        "art-2",
        "Portishead",
        "8f6bd1e4-fbe1-4f50-aa9b-94c450ec0f11",
        genres=[],
    ),
    "art-3": _artist("art-3", "Nobody At All", None),
}

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
    """What the app sent, so a test can assert on the exact body."""
    return {"writes": WRITES, "items": LIBRARY}


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


@app.get("/Artists")
async def artists(request: Request) -> dict[str, Any]:
    # `includeItemTypes` empties the result on a real server.
    if request.query_params.get("includeItemTypes"):
        return {"Items": [], "TotalRecordCount": 0}
    term = (request.query_params.get("searchTerm") or "").lower()
    if term and "radiohead" not in term:
        return {"Items": [], "TotalRecordCount": 0}
    items = list(LIBRARY.values())
    return {"Items": items, "TotalRecordCount": len(items)}


@app.get("/Items")
async def items(request: Request) -> dict[str, Any]:
    raw_ids = request.query_params.get("ids")
    if raw_ids:
        found = [LIBRARY[i] for i in raw_ids.split(",") if i in LIBRARY]
    else:
        found = list(LIBRARY.values())
        wanted = (request.query_params.get("includeItemTypes") or "").strip()
        if wanted:
            types = {name for name in wanted.split(",") if name}
            found = [row for row in found if row.get("Type") in types]
        term = (request.query_params.get("searchTerm") or "").lower()
        if term:
            found = [row for row in found if term in str(row.get("Name", "")).lower()]
        offset = request.query_params.get("startIndex")
        if offset and str(offset).isdigit():
            found = found[int(offset) :]
        limit = request.query_params.get("limit")
        if limit and str(limit).isdigit():
            found = found[: int(limit)]
    return {"Items": found, "TotalRecordCount": len(found)}


@app.get("/Items/{item_id}")
async def item(item_id: str, request: Request) -> JSONResponse:
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
