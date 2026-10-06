"""A faithful in-process Jellyfin, for tests that must not need a live server.

Every bug in this project that reached a user was found by talking to the real server, and
**none** were found by the test suite. The reason is visible in this repository's history:
each integration test carried its own handful of route branches that answered whatever the
test wanted, so the mocks agreed with the client's assumptions instead of the server's
behaviour. A mock that accepts everything cannot fail, and a test that cannot fail is not
verification.

This module inverts that. It reproduces what Jellyfin 12.2.0 actually does, including the
parts that cost real debugging time, and it *rejects* what the real server rejects:

* authentication is the ``MediaBrowser`` scheme; a bare ``Authorization: <key>`` is 401;
* an API key is userless, so ``GET /Users/Me`` is **400**;
* ``GET /Items/{itemId}`` is **400** without a ``userId`` -- for every id, including one
  the server itself just returned -- while the list endpoints tolerate its absence;
* ``GET /Artists`` returns *nothing* when given ``includeItemTypes``;
* ``GET /Library/MediaFolders`` omits ``ItemId``; ``/Library/VirtualFolders`` has it;
* an ``Etag`` is only present when it was asked for via ``fields=``;
* ``POST /Items/{itemId}`` is a **full overwrite**: a key absent from the body becomes
  null. This is the hazard the whole tool is shaped around, so the mock performs it rather
  than merging.

Each behaviour is a named flag rather than hard-coded, so a test that is genuinely about
something else can switch one off -- and, more usefully, can assert that the client
*works around* it. The defaults are the faithful ones: a test that forgets to think about
a quirk gets the real behaviour, not a convenient one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx

# The writable field set, duplicated deliberately rather than imported from
# `metaedit.domain.writable`: importing the app's own definition would make the mock agree
# with the app by construction, so a bug in that definition would be invisible here.
WRITABLE_FIELDS: tuple[str, ...] = (
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


@dataclass
class Recording:
    """What the mock was asked to do, so a test can assert on the exact request."""

    requests: list[httpx.Request] = field(default_factory=list)
    writes: list[dict[str, Any]] = field(default_factory=list)

    def paths(self) -> list[str]:
        return [request.url.path for request in self.requests]

    def count(self, path: str) -> int:
        return sum(1 for path_seen in self.paths() if path_seen == path)


def artist(
    item_id: str,
    name: str,
    *,
    mbid: str | None = None,
    genres: list[str] | None = None,
    overview: str = "",
    tags: list[str] | None = None,
    locked: list[str] | None = None,
) -> dict[str, Any]:
    """A `MusicArtist` as the server sends one."""
    return {
        "Id": item_id,
        "Type": "MusicArtist",
        "Name": name,
        "Etag": f"etag-{item_id}-1",
        "SourceType": "Library",
        "Genres": genres or [],
        "Tags": tags if tags is not None else ["keep-me"],
        "Studios": [],
        "ProductionLocations": [],
        "ProviderIds": {"MusicBrainzArtist": mbid} if mbid else {},
        "ExternalUrls": [],
        "LockedFields": locked or [],
        "People": [],
        "Overview": overview,
        "LockData": False,
    }


def album(
    item_id: str,
    name: str,
    album_artist: str,
    *,
    mbid: str | None = None,
    genres: list[str] | None = None,
    overview: str = "",
) -> dict[str, Any]:
    """A `MusicAlbum`, with the scalar `AlbumArtist` spelling the server often uses."""
    return {
        "Id": item_id,
        "Type": "MusicAlbum",
        "Name": name,
        "Etag": f"etag-{item_id}-1",
        "SourceType": "Library",
        "AlbumArtist": album_artist,
        "Artists": [album_artist],
        "Genres": genres or [],
        "Tags": ["keep-me"],
        "Studios": [],
        "ProductionLocations": [],
        "ProviderIds": {"MusicBrainzAlbum": mbid} if mbid else {},
        "ExternalUrls": [],
        "LockedFields": [],
        "People": [],
        "Overview": overview,
        "LockData": False,
    }


class FakeJellyfin:
    """An in-process Jellyfin that behaves like the real one.

    Mount it with :meth:`transport`. Every quirk is a flag so a test can either rely on it
    or turn it off and assert the client compensates.
    """

    def __init__(
        self,
        items: list[dict[str, Any]] | None = None,
        *,
        api_key: str = "test-admin-key",
        # Faithful behaviours, each verified live against 12.2.0.
        require_mediabrowser_auth: bool = True,
        api_key_is_userless: bool = True,
        require_user_for_single_item: bool = True,
        artists_emptied_by_include_item_types: bool = True,
        media_folders_omit_item_id: bool = True,
        etag_only_when_requested: bool = True,
        # Knobs for negative tests.
        fail_writes_with: int | None = None,
        write_error_body: dict[str, Any] | None = None,
    ) -> None:
        self.library: dict[str, dict[str, Any]] = {item["Id"]: item for item in (items or [])}
        self.users: list[dict[str, Any]] = [
            {"Id": "user-1", "Name": "admin", "Policy": {"IsAdministrator": True}}
        ]
        self.api_key = api_key
        self.require_mediabrowser_auth = require_mediabrowser_auth
        self.api_key_is_userless = api_key_is_userless
        self.require_user_for_single_item = require_user_for_single_item
        self.artists_emptied_by_include_item_types = artists_emptied_by_include_item_types
        self.media_folders_omit_item_id = media_folders_omit_item_id
        self.etag_only_when_requested = etag_only_when_requested
        self.fail_writes_with = fail_writes_with
        self.write_error_body = write_error_body
        self.recording = Recording()

    # ---------------------------------------------------------------- mounting

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    # ----------------------------------------------------------------- helpers

    def add(self, item: dict[str, Any]) -> dict[str, Any]:
        self.library[item["Id"]] = item
        return item

    def stored(self, item_id: str) -> dict[str, Any]:
        """What the server currently holds, so a test can assert on the result."""
        return self.library[item_id]

    @property
    def writes(self) -> list[dict[str, Any]]:
        return self.recording.writes

    def last_write(self) -> dict[str, Any]:
        return self.recording.writes[-1]["body"]

    # ------------------------------------------------------------------- auth

    def _authorised(self, request: httpx.Request) -> bool:
        header = request.headers.get("authorization", "")
        if not self.require_mediabrowser_auth:
            return True
        # Only the MediaBrowser scheme is accepted, as on a real 12.x server: a bare token
        # is parsed as a malformed scheme and rejected.
        if not header.startswith("MediaBrowser "):
            return False
        return f'Token="{self.api_key}"' in header

    # ----------------------------------------------------------------- routing

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.recording.requests.append(request)
        path = request.url.path

        if path in ("/System/Info/Public", "/System/Info"):
            if path == "/System/Info/Public" or self._authorised(request):
                return httpx.Response(
                    200, json={"ServerName": "fake", "Version": "12.2.0", "Id": "fake-server"}
                )
            return httpx.Response(401, json={"title": "Unauthorized"})

        if not self._authorised(request):
            return httpx.Response(401, json={"title": "Unauthorized"})

        handler = _ROUTES.get(path) or _single_item
        return handler(self, request, path)

    # ------------------------------------------------------------------- items

    def _read_item(self, request: httpx.Request, path: str) -> httpx.Response:
        # A userless credential needs an explicit user, and the endpoint 400s without one
        # rather than 404ing or ignoring it. This is the behaviour that broke the whole
        # edit path: it looks like a bad id, and it happens for every id.
        if self.require_user_for_single_item and not request.url.params.get("userId"):
            return httpx.Response(400, text="Error processing request.")
        item_id = path.split("/")[2]
        row = self.library.get(item_id)
        if row is None:
            return httpx.Response(404, json={"title": "Not Found"})
        payload = dict(row)
        if self.etag_only_when_requested:
            fields = request.url.params.get("fields") or ""
            if "Etag" not in fields:
                payload.pop("Etag", None)
        return httpx.Response(200, json=payload)

    def _update_item(self, request: httpx.Request, path: str) -> httpx.Response:
        item_id = path.split("/")[2]
        if self.fail_writes_with is not None:
            return httpx.Response(
                self.fail_writes_with,
                json=self.write_error_body
                or {"title": "Bad Request", "status": self.fail_writes_with},
            )
        row = self.library.get(item_id)
        if row is None:
            return httpx.Response(404, json={"title": "Not Found"})
        body = json.loads(request.content or b"{}")
        self.recording.writes.append({"item_id": item_id, "body": body})

        # A genuine full overwrite: every writable key absent from the body becomes null.
        # Merging instead would hide the single most dangerous failure mode this tool has.
        for name in WRITABLE_FIELDS:
            row[name] = body.get(name)
        for key, value in body.items():
            row[key] = value
        counter = len(self.recording.writes)
        row["Etag"] = f"etag-{item_id}-{counter + 1}"
        return httpx.Response(204)


# --------------------------------------------------------------------- routes


def _single_item(fake: FakeJellyfin, request: httpx.Request, path: str) -> httpx.Response:
    if not path.startswith("/Items/"):
        return httpx.Response(404, json={"title": "Not Found"})
    if request.method == "POST":
        return fake._update_item(request, path)
    return fake._read_item(request, path)


def _users_me(fake: FakeJellyfin, request: httpx.Request, path: str) -> httpx.Response:
    """A userless API key answers 400 here -- a *success* signal for that credential.

    Jellyfin returns a generic RFC 9110 problem document with no reason text, so a client
    cannot tell "userless" from "malformed" by reading it. That is why the app confirms the
    credential with a second authenticated read instead.
    """
    if fake.api_key_is_userless:
        return httpx.Response(
            400,
            json={
                "type": "https://tools.ietf.org/html/rfc9110#section-15.5.1",
                "title": "Bad Request",
                "status": 400,
            },
        )
    return httpx.Response(200, json=fake.users[0])


def _users(fake: FakeJellyfin, request: httpx.Request, path: str) -> httpx.Response:
    return httpx.Response(200, json=fake.users)


def _artists(fake: FakeJellyfin, request: httpx.Request, path: str) -> httpx.Response:
    """`/Artists` already returns artists; filtering it empties the result."""
    if fake.artists_emptied_by_include_item_types and request.url.params.get("includeItemTypes"):
        return httpx.Response(200, json={"Items": [], "TotalRecordCount": 0})
    rows = [row for row in fake.library.values() if row.get("Type") == "MusicArtist"]
    return _paged(request, rows)


def _items(fake: FakeJellyfin, request: httpx.Request, path: str) -> httpx.Response:
    params = request.url.params
    raw_ids = params.get("ids")
    if raw_ids:
        rows = [fake.library[i] for i in raw_ids.split(",") if i in fake.library]
        return httpx.Response(200, json={"Items": rows, "TotalRecordCount": len(rows)})

    rows = list(fake.library.values())
    wanted = (params.get("includeItemTypes") or "").strip()
    if wanted:
        types = {name for name in wanted.split(",") if name}
        rows = [row for row in rows if row.get("Type") in types]
    term = (params.get("searchTerm") or "").lower()
    if term:
        rows = [row for row in rows if term in str(row.get("Name", "")).lower()]
    # Unlike a single-item read, the list form is happy without a user.
    return _paged(request, rows)


def _paged(request: httpx.Request, rows: list[dict[str, Any]]) -> httpx.Response:
    total = len(rows)
    offset = request.url.params.get("startIndex")
    if offset and str(offset).isdigit():
        rows = rows[int(offset) :]
    limit = request.url.params.get("limit")
    if limit and str(limit).isdigit():
        rows = rows[: int(limit)]
    return httpx.Response(200, json={"Items": rows, "TotalRecordCount": total})


def _virtual_folders(fake: FakeJellyfin, request: httpx.Request, path: str) -> httpx.Response:
    """The endpoint that actually carries `ItemId`, as a bare list."""
    return httpx.Response(
        200,
        json=[
            {
                "Name": "Music",
                "ItemId": "lib-music",
                "CollectionType": "music",
                "Locations": ["/media/Music"],
            }
        ],
    )


def _media_folders(fake: FakeJellyfin, request: httpx.Request, path: str) -> httpx.Response:
    """The endpoint whose `ItemId` is null on a real server."""
    return httpx.Response(
        200,
        json={
            "Items": [
                {
                    "Name": "Music",
                    "ItemId": None if fake.media_folders_omit_item_id else "lib-music",
                    "CollectionType": "music",
                }
            ]
        },
    )


# Assembled last: the dict names the functions above, so building it earlier would
# reference them before they exist.
_ROUTES: dict[str, Any] = {
    "/Users/Me": _users_me,
    "/Users": _users,
    "/Artists": _artists,
    "/Items": _items,
    "/Library/VirtualFolders": _virtual_folders,
    "/Library/MediaFolders": _media_folders,
}
