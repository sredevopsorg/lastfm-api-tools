"""A stub Last.fm for end-to-end tests.

The end-to-end journey *writes* metadata, so it must not depend on a third-party service
being up, and it must not spend a real rate limit -- a suite that needs the internet to
pass is a suite that fails for reasons unrelated to the code.

The shapes here mirror what the live API actually returns, including the awkward parts
that cost real debugging time:

* ``getTopTags`` is **envelope-free** -- ``{"toptags": {...}}`` with no entity wrapper --
  which is why the entity derivation skips it and a separate reader is needed.
* its counts arrive as integers in JSON but the same API serves them as strings in XML,
  so the stub can be told to send either.
* the ``@attr`` block carries the attribution (``artist``, and for albums and tracks the
  second component too), which is what the counts are keyed by.
* ``getInfo`` returns a tag list **without** counts, so popularity can only come from
  the TopTags call. A stub that put counts there would hide the whole bug class.
"""

from __future__ import annotations

import os
import sys
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="stub-lastfm")

API_KEY = os.environ.get("STUB_LASTFM_KEY", "e2e-lastfm-key")
# When set, TopTags sends its counts as strings, exercising the tolerance the real XML
# endpoint requires.
STRING_COUNTS = os.environ.get("STUB_STRING_COUNTS") == "1"

CALLS: list[dict[str, Any]] = []

ARTISTS: dict[str, dict[str, Any]] = {
    "radiohead": {
        "name": "Radiohead",
        "mbid": "a74b1b7f-71a5-4011-9441-d0b5e4122711",
        "tags": ["rock", "alternative"],
        "toptags": [("alternative rock", 100), ("rock", 89), ("art rock", 41), ("electronic", 11)],
        "similar": [("Thom Yorke", 1.0), ("Atoms for Peace", 0.485), ("Jeff Buckley", 0.316)],
        "bio": (
            "Radiohead are an English rock band formed in Abingdon, Oxfordshire, in 1985. "
            "They are known for an experimental approach that has kept them difficult to "
            "categorise across three decades of releases."
        ),
    },
    "portishead": {
        "name": "Portishead",
        "mbid": "8f6bd1e4-fbe1-4f50-aa9b-94c450ec0f11",
        "tags": ["trip hop", "electronic"],
        "toptags": [("trip hop", 100), ("electronic", 72), ("downtempo", 38)],
        "similar": [("Massive Attack", 0.71), ("Tricky", 0.42)],
        "bio": (
            "Portishead are an English band formed in Bristol in 1991, named after the "
            "nearby town and central to the sound that became known as trip hop."
        ),
    },
}

ALBUMS: dict[tuple[str, str], dict[str, Any]] = {
    ("radiohead", "ok computer"): {
        "name": "OK Computer",
        "artist": "Radiohead",
        "tags": ["alternative rock", "rock"],
        "toptags": [("alternative rock", 100), ("rock", 89), ("art rock", 44)],
        "tracks": ["Airbag", "Paranoid Android", "Subterranean Homesick Alien"],
        "wiki": (
            "OK Computer is the third studio album by the English rock band Radiohead, "
            "released in 1997. It was recorded in rural Oxfordshire and is widely regarded "
            "as one of the defining records of its decade."
        ),
    },
}


def _guard(request: Request) -> JSONResponse | None:
    if request.query_params.get("api_key") != API_KEY:
        return JSONResponse(
            {"error": 10, "message": "Invalid API key - You must be granted a valid key"},
            status_code=403,
        )
    return None


def _count(value: int) -> int | str:
    return str(value) if STRING_COUNTS else value


def _tag_list(entries: list[tuple[str, int]]) -> list[dict[str, Any]]:
    return [
        {
            "url": f"https://www.last.fm/tag/{name.replace(' ', '+')}",
            "name": name,
            "count": _count(n),
        }
        for name, n in entries
    ]


def _plain_tags(names: list[str]) -> dict[str, Any]:
    """The `getInfo` tag list: names and urls, deliberately **no counts**."""
    return {
        "tag": [
            {"url": f"https://www.last.fm/tag/{name.replace(' ', '+')}", "name": name}
            for name in names
        ]
    }


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/2.0/")
@app.get("/2.0")
async def api(request: Request) -> JSONResponse:
    guard = _guard(request)
    if guard is not None:
        return guard
    params = request.query_params
    method = params.get("method", "")
    CALLS.append({"method": method, "params": dict(params)})

    artist_name = (params.get("artist") or "").strip()
    artist = ARTISTS.get(artist_name.lower())

    if method == "artist.getinfo":
        if not artist:
            return _not_found("Artist not found")
        return JSONResponse(
            {
                "artist": {
                    "name": artist["name"],
                    "mbid": artist["mbid"],
                    "url": f"https://www.last.fm/music/{artist['name'].replace(' ', '+')}",
                    "stats": {"listeners": "5000000", "playcount": "100000000"},
                    "tags": _plain_tags(artist["tags"]),
                    "bio": {"summary": artist["bio"], "published": "Thu, 13 Mar 2008"},
                }
            }
        )

    if method == "artist.gettoptags":
        if not artist:
            return _not_found("Artist not found")
        return JSONResponse(
            {"toptags": {"@attr": {"artist": artist["name"]}, "tag": _tag_list(artist["toptags"])}}
        )

    if method == "artist.getsimilar":
        if not artist:
            return _not_found("Artist not found")
        # The owner is only on the container attribute; the peer list owns the "artist"
        # key, which is why attribution cannot come from the body's shape alone.
        peers = [
            {"name": name, "match": str(match), "url": f"https://www.last.fm/music/{name}"}
            for name, match in artist["similar"]
        ]
        return JSONResponse(
            {"similarartists": {"@attr": {"artist": artist["name"]}, "artist": peers}}
        )

    if method == "album.getinfo":
        album_name = (params.get("album") or "").strip().lower()
        album = ALBUMS.get((artist_name.lower(), album_name))
        if not album:
            return _not_found("Album not found")
        return JSONResponse(
            {
                "album": {
                    "name": album["name"],
                    "artist": album["artist"],
                    "url": "https://www.last.fm/music/Radiohead/OK+Computer",
                    "tags": _plain_tags(album["tags"]),
                    "wiki": {"summary": album["wiki"], "published": "11 Nov 2022"},
                    "tracks": {
                        "track": [
                            {"name": name, "@attr": {"rank": str(i + 1)}}
                            for i, name in enumerate(album["tracks"])
                        ]
                    },
                }
            }
        )

    if method == "album.gettoptags":
        album_name = (params.get("album") or "").strip().lower()
        album = ALBUMS.get((artist_name.lower(), album_name))
        if not album:
            return _not_found("Album not found")
        return JSONResponse(
            {
                "toptags": {
                    "@attr": {"artist": album["artist"], "album": album["name"]},
                    "tag": _tag_list(album["toptags"]),
                }
            }
        )

    if method in ("track.getinfo", "track.gettoptags", "track.getsimilar"):
        # Tracks are not seeded: the point is that a plausible method reaches the stub
        # and is answered, including the not-found path the harvest must survive.
        return _not_found("Track not found")

    if method in ("artist.search", "album.search", "track.search"):
        term = (params.get("artist") or params.get("album") or "").strip().lower()
        hits = [
            {
                "name": a["name"],
                "mbid": a["mbid"],
                "url": "https://www.last.fm/music/x",
                "listeners": 100,
            }
            for key, a in ARTISTS.items()
            if term and term[:4] in key
        ]
        envelope = {"artist": hits} if method == "artist.search" else {"album": hits}
        return JSONResponse({"results": {"opensearch:totalResults": str(len(hits)), **envelope}})

    return JSONResponse({"error": 3, "message": f"Invalid method - {method}"}, status_code=400)


def _not_found(message: str) -> JSONResponse:
    """Error 6, which the client maps to LastfmNotFound rather than a failure."""
    return JSONResponse({"error": 6, "message": message}, status_code=404)


@app.get("/__calls")
async def calls() -> dict[str, Any]:
    return {"calls": CALLS}


@app.post("/__reset")
async def reset() -> dict[str, str]:
    CALLS.clear()
    return {"status": "reset"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app, host="0.0.0.0", port=int(os.environ.get("STUB_PORT", "8077")), log_level="warning"
    )
    sys.exit(0)
