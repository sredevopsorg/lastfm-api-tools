"""Live tests against a real Jellyfin server.

**Read-only.** Nothing here writes: no ``POST /Items/{itemId}``, no refresh, no
library scan. The write path is exercised for the first time in phase 5.

Skipped unless ``JELLYFIN_URL`` and ``JELLYFIN_API_KEY`` are set. These exist to
settle four assumptions that the whole read path rests on and that fixtures cannot
answer:

1. which ``Authorization`` header form the server actually accepts;
2. whether ``Etag`` is present and changes on write (existence is checked here);
3. whether ``Genres`` writes create genre entities immediately (needs a write, so
   only *reported* here, not asserted);
4. **whether every field requested via ``fields=`` is actually returned** — the one
   that matters most, because the write payload is built from a fresh read and a
   field the server omits cannot be round-tripped.

Run with::

    uv run pytest -m live_jellyfin -v -s     # uses JELLYFIN_* from .env
"""

from __future__ import annotations

import httpx
import pytest

from metaedit.adapters.jellyfin.client import ITEM_FIELDS, JellyfinClient
from metaedit.config import Settings
from metaedit.config import get_settings as _get_settings
from metaedit.domain.writable import payload_fields

_settings_at_import = _get_settings()
JELLYFIN_URL = _settings_at_import.jellyfin_url
JELLYFIN_KEY = _settings_at_import.jellyfin_key()

pytestmark = [
    pytest.mark.live_jellyfin,
    pytest.mark.skipif(
        not (JELLYFIN_URL and JELLYFIN_KEY),
        reason="JELLYFIN_URL and JELLYFIN_API_KEY are not both set",
    ),
]


def _settings(**overrides: object) -> Settings:
    defaults: dict[str, object] = {
        "_env_file": None,
        "JELLYFIN_URL": JELLYFIN_URL,
        "JELLYFIN_API_KEY": JELLYFIN_KEY,
        "LOG_JSON": False,
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


@pytest.fixture
async def client() -> httpx.AsyncClient:  # type: ignore[misc]
    async with JellyfinClient(_settings()) as instance:
        yield instance


async def test_server_is_reachable_and_reports_a_version(client: JellyfinClient) -> None:
    info = await client.system_info_public()
    print(f"\nserver: {info.ServerName!r} version {info.Version!r}")
    assert info.Version, "a reachable Jellyfin must report its version"


async def test_authentication_header_form(client: JellyfinClient) -> None:
    """Assumption 1: which header spelling the server accepts.

    ``JellyfinClient`` already tries both and remembers the winner, so this asserts
    that one of them works and prints which. If both fail, the key is wrong or the
    header scheme differs from the OpenAPI contract.
    """
    user = await client.current_user()
    assert user.Name, "an authenticated call must identify the user"
    print(f"\nauthenticated as {user.Name!r} (admin={user.Policy.IsAdministrator})")
    print(f"  header form that worked: {client._auth_header}")

    allowed, reason = await client.can_write_metadata()
    print(f"  can write metadata: {allowed} ({reason})")
    if not allowed:
        pytest.skip(
            f"the key is not elevated ({reason}); read endpoints still work, but the "
            "write path cannot be exercised. Phase 5 needs an administrator key."
        )


async def test_music_libraries_are_visible(client: JellyfinClient) -> None:
    libraries = await client.music_libraries()
    print(f"\nmusic libraries: {[library.Name for library in libraries]}")
    if not libraries:
        pytest.skip("no music library on this server, so nothing to inspect")
    assert libraries


async def test_artist_items_are_readable(client: JellyfinClient) -> None:
    result = await client.artists(limit=5)
    print(f"\nartists: total={result.TotalRecordCount} first={[i.Name for i in result.Items][:5]}")
    if not result.Items:
        pytest.skip("the music library has no artists")
    assert result.Items[0].Id


async def test_every_requested_field_is_returned(client: JellyfinClient) -> None:
    """Assumption 4, and the important one.

    The write payload must contain every whitelisted field, or the server nulls it.
    That payload is built from a fresh read, so any whitelist field the read does
    *not* return becomes `None` and would be written back as a cleared value -- the
    exact failure ADR 0003 exists to prevent.

    This reports precisely which fields the server omits, per item kind.
    """
    found: dict[str, list[dict[str, object]]] = {}
    for kind, label in (("MusicArtist", "artist"), ("MusicAlbum", "album"), ("Audio", "song")):
        result = await client.items(kind=kind, limit=3)  # type: ignore[arg-type]
        if not result.Items:
            continue
        for dto in result.Items:
            raw = dto.model_dump()
            required = payload_fields(kind)  # type: ignore[arg-type]
            missing = sorted(field for field in required if field not in raw)
            found[label] = [{"item": dto.Name, "missing": missing}]
            print(f"\n{label} {dto.Name!r}: {len(required) - len(missing)}/{len(required)} present")
            if missing:
                print(f"  OMITTED BY SERVER: {missing}")

    if not found:
        pytest.skip("no music items on this server to inspect")

    problems = {label: rows for label, rows in found.items() if rows[0]["missing"]}
    assert not problems, (
        "the server omitted whitelist fields, so a read cannot round-trip them and "
        f"an apply would clear them: {problems}. Either request them differently or "
        "remove them from the payload field set."
    )


async def test_item_fields_requests_are_honoured(client: JellyfinClient) -> None:
    """The ``fields=`` values we depend on must come back when asked for."""
    result = await client.artists(limit=1)
    if not result.Items:
        pytest.skip("no artists to inspect")
    raw = result.Items[0].model_dump()
    honoured = sorted(field for field in ITEM_FIELDS if field in raw)
    ignored = sorted(field for field in ITEM_FIELDS if field not in raw)
    print(f"\nfields= honoured: {honoured}")
    if ignored:
        print(f"fields= ignored (defaults may still cover these): {ignored}")
    # Presence of the object itself is non-negotiable.
    assert raw.get("Id"), "an item must always come back with its id"


async def test_etag_is_present(client: JellyfinClient) -> None:
    """Assumption 2, partially: optimistic concurrency needs an Etag at all.

    Whether it *changes* after a write cannot be checked without writing, which is
    phase 5's job. If items carry no Etag, the fallback is a hash of the whitelist
    fields plus ``DateLastSaved`` (ADR 0007).
    """
    result = await client.artists(limit=5)
    if not result.Items:
        pytest.skip("no artists to inspect")
    with_etag = [item.Name for item in result.Items if item.Etag]
    print(f"\nitems with an Etag: {len(with_etag)}/{len(result.Items)}")
    print(f"  DateLastSaved present: {sum(1 for i in result.Items if i.DateLastSaved)}")
    if not with_etag:
        pytest.skip("no Etag on music items; the ADR 0007 fallback will be needed")


async def test_item_type_names_match_what_we_query(client: JellyfinClient) -> None:
    """We query ``includeItemTypes=MusicArtist|MusicAlbum|Audio``.

    If a server uses different names, every browse silently returns nothing -- the
    same absence-shaped failure as an unrecognised payload, so it is worth asserting
    against a live server rather than trusting the vendored enum.
    """
    info = await client.system_info_public()
    print(f"\nserver version {info.Version} vs vendored spec 12.2.0")
    if info.Version and not info.Version.startswith("12."):
        print(
            "  WARNING: server version differs from the vendored spec. The contract "
            "tests assert against the spec, not this server."
        )

    for kind in ("MusicArtist", "MusicAlbum", "Audio"):
        result = await client.items(kind=kind, limit=1)  # type: ignore[arg-type]
        print(f"  {kind}: {result.TotalRecordCount} item(s)")
        if result.Items:
            # The server echoed an item of this kind, so the name is understood.
            assert result.Items[0].Type == kind, (
                f"queried {kind} but the server returned {result.Items[0].Type!r}"
            )
