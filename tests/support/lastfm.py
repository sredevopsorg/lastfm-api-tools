"""A faithful in-process Last.fm, for tests that must not need the internet.

Reproduces what the live API actually does, including the awkward parts, so the mocks
disagree with a wrong client instead of agreeing with it:

* ``getTopTags`` is **envelope-free** -- ``{"toptags": {...}}`` with no entity wrapper --
  which is why the entity derivation skips it and a separate reader is needed. A mock that
  wrapped it would hide that entirely.
* ``getInfo`` returns a tag list **without counts**, so popularity can only come from the
  top-tags call. A mock that put counts in ``getInfo`` would hide the bug class that made
  every tag count null for weeks.
* **Last.fm is inconsistent with itself about punctuation.** For one real album
  ``album.getInfo`` returned ``"\u2026and Justice for All"`` (U+2026) while
  ``album.getTopTags`` returned ``"...and Justice for All"`` (three full stops), which
  silently dropped that album's popularity until the join was made tolerant. The mock
  reproduces the discrepancy on demand because it is the only way to test the tolerance.
* Errors travel as an ``error`` code in the body, not as an HTTP status: 6 is not-found,
  10 is a rejected key. A client that keys off the status instead gets this wrong, and the
  mock will not rescue it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

NOT_FOUND = 6
INVALID_KEY = 10


@dataclass
class Recording:
    calls: list[dict[str, Any]] = field(default_factory=list)

    def methods(self) -> list[str]:
        return [call["method"] for call in self.calls]

    def count(self, method: str) -> int:
        return sum(1 for seen in self.methods() if seen == method)


def _tags(names: list[str]) -> dict[str, Any]:
    """The `getInfo` tag list: names and urls, deliberately **no counts**."""
    return {
        "tag": [
            {"url": f"https://www.last.fm/tag/{name.replace(' ', '+')}", "name": name}
            for name in names
        ]
    }


class FakeLastfm:
    """An in-process Last.fm, mounted with :meth:`transport`."""

    def __init__(
        self,
        *,
        api_key: str = "test-lastfm-key",
        # Faithful behaviours.
        top_tags_are_envelope_free: bool = True,
        # Reproduce Last.fm's own punctuation inconsistency between endpoints.
        info_ellipsis: str = "\u2026",
        top_tags_ellipsis: str = "...",
        # When set, top-tag counts are sent as strings, as the XML-derived responses do.
        string_counts: bool = False,
    ) -> None:
        self.api_key = api_key
        self.top_tags_are_envelope_free = top_tags_are_envelope_free
        self.info_ellipsis = info_ellipsis
        self.top_tags_ellipsis = top_tags_ellipsis
        self.string_counts = string_counts

        self.artists: dict[str, dict[str, Any]] = {}
        self.albums: dict[tuple[str, str], dict[str, Any]] = {}
        self.fail_with_code: int | None = None
        self.recording = Recording()

    # ---------------------------------------------------------------- mounting

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    # ----------------------------------------------------------------- fixtures

    def add_artist(
        self,
        name: str,
        *,
        mbid: str | None = None,
        tags: list[str] | None = None,
        top_tags: list[tuple[str, int]] | None = None,
        similar: list[tuple[str, float]] | None = None,
        bio: str = (
            "A band with a biography long enough to satisfy the overview policy, which "
            "requires a minimum length before it will propose one."
        ),
    ) -> dict[str, Any]:
        entry = {
            "name": name,
            "mbid": mbid,
            "tags": tags or [],
            "top_tags": top_tags or [],
            "similar": similar or [],
            "bio": bio,
        }
        self.artists[name.lower()] = entry
        return entry

    def add_album(
        self,
        artist: str,
        name: str,
        *,
        mbid: str | None = None,
        tags: list[str] | None = None,
        top_tags: list[tuple[str, int]] | None = None,
        wiki: str | None = None,
    ) -> dict[str, Any]:
        entry = {
            "artist": artist,
            "name": name,
            "mbid": mbid,
            "tags": tags or [],
            "top_tags": top_tags or [],
            "wiki": wiki,
        }
        self.albums[(artist.lower(), name.lower())] = entry
        return entry

    @property
    def calls(self) -> list[dict[str, Any]]:
        return self.recording.calls

    # ------------------------------------------------------------------ routing

    def handle(self, request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        method = params.get("method", "")
        self.recording.calls.append({"method": method, "params": params})

        if params.get("api_key") != self.api_key:
            return _error(INVALID_KEY, "Invalid API key")
        if self.fail_with_code is not None:
            return _error(self.fail_with_code, "forced failure")

        handler = getattr(self, f"_on_{method.replace('.', '_')}", None)
        if handler is None:
            return _error(3, f"Invalid method - {method}")
        return handler(params)

    # --------------------------------------------------------------- endpoints

    def _on_artist_getinfo(self, params: dict[str, str]) -> httpx.Response:
        entry = self.artists.get((params.get("artist") or "").lower())
        if entry is None:
            return _not_found("Artist not found")
        return httpx.Response(
            200,
            json={
                "artist": {
                    "name": entry["name"],
                    "mbid": entry["mbid"],
                    "stats": {"listeners": "1000", "playcount": "5000"},
                    "tags": _tags(entry["tags"]),
                    "bio": {"summary": entry["bio"], "published": "Thu, 13 Mar 2008"},
                }
            },
        )

    def _on_artist_gettoptags(self, params: dict[str, str]) -> httpx.Response:
        entry = self.artists.get((params.get("artist") or "").lower())
        if entry is None:
            return _not_found("Artist not found")
        return httpx.Response(
            200,
            json=self._toptags({"artist": entry["name"]}, entry["top_tags"]),
        )

    def _on_artist_getsimilar(self, params: dict[str, str]) -> httpx.Response:
        entry = self.artists.get((params.get("artist") or "").lower())
        if entry is None:
            return _not_found("Artist not found")
        # The owner rides on the container attribute while the peer list owns the "artist"
        # key, so a flattened body is ambiguous -- the reason the client attributes from
        # the request params instead.
        peers = [
            {"name": name, "match": str(match), "url": f"https://www.last.fm/music/{name}"}
            for name, match in entry["similar"]
        ]
        return httpx.Response(
            200, json={"similarartists": {"@attr": {"artist": entry["name"]}, "artist": peers}}
        )

    def _on_album_getinfo(self, params: dict[str, str]) -> httpx.Response:
        entry = self._album(params)
        if entry is None:
            return _not_found("Album not found")
        return httpx.Response(
            200,
            json={
                "album": {
                    # The punctuation the real API uses here, which differs from the
                    # top-tags endpoint's for the same album.
                    "name": f"{self.info_ellipsis}and Justice for All"
                    if entry["name"] == "and Justice for All"
                    else entry["name"],
                    "artist": entry["artist"],
                    "mbid": entry["mbid"],
                    "tags": _tags(entry["tags"]),
                    "wiki": {"summary": entry["wiki"] or ""} if entry["wiki"] else None,
                    "tracks": {"track": []},
                }
            },
        )

    def _on_album_gettoptags(self, params: dict[str, str]) -> httpx.Response:
        entry = self._album(params)
        if entry is None:
            return _not_found("Album not found")
        name = (
            f"{self.top_tags_ellipsis}and Justice for All"
            if entry["name"] == "and Justice for All"
            else entry["name"]
        )
        return httpx.Response(
            200,
            json=self._toptags({"artist": entry["artist"], "album": name}, entry["top_tags"]),
        )

    def _on_track_getinfo(self, params: dict[str, str]) -> httpx.Response:
        return _not_found("Track not found")

    def _on_track_gettoptags(self, params: dict[str, str]) -> httpx.Response:
        return _not_found("Track not found")

    # ----------------------------------------------------------------- helpers

    def _album(self, params: dict[str, str]) -> dict[str, Any] | None:
        key = ((params.get("artist") or "").lower(), (params.get("album") or "").lower())
        entry = self.albums.get(key)
        if entry is not None:
            return entry
        mbid = params.get("mbid")
        if mbid:
            for candidate in self.albums.values():
                if candidate["mbid"] == mbid:
                    return candidate
        return None

    def _toptags(self, attr: dict[str, str], entries: list[tuple[str, int]]) -> dict[str, Any]:
        tags = [
            {
                "url": f"https://www.last.fm/tag/{name.replace(' ', '+')}",
                "name": name,
                "count": str(count) if self.string_counts else count,
            }
            for name, count in entries
        ]
        body: dict[str, Any] = {"@attr": attr, "tag": tags}
        if self.top_tags_are_envelope_free:
            return {"toptags": body}
        return {"toptags": body, "@attr": attr}  # the fiction a wrong client expects


def _error(code: int, message: str) -> httpx.Response:
    """Errors travel in the body, as the real API does."""
    return httpx.Response(200, json={"error": code, "message": message})


def _not_found(message: str) -> httpx.Response:
    return _error(NOT_FOUND, message)


def tags_without_counts(body: dict[str, Any]) -> bool:
    """True if a `getInfo` body's tag list carries no counts, as the real one does not."""
    tags = ((body.get("artist") or body.get("album") or {}).get("tags") or {}).get("tag") or []
    return all("count" not in tag for tag in tags)
