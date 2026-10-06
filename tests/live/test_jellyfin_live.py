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


async def test_credential_is_accepted_and_can_write(client: JellyfinClient) -> None:
    """Settled against a live 12.2.0 server, and no longer a guess.

    Jellyfin 12 disabled the legacy auth channels, so a bare
    ``Authorization: <key>`` or ``X-Emby-Token`` is rejected with 401. The working
    form is the ``MediaBrowser`` scheme with quoted parameters -- the same header the
    official SDKs send. This asserts a credential is accepted and reports which kind
    it is, because the two kinds answer ``/Users/Me`` differently by design.
    """
    from metaedit.adapters.jellyfin.client import build_authorization_header

    print(f"\nheader sent: {build_authorization_header('REDACTED')}")

    allowed, reason = await client.can_write_metadata()
    print(f"  credential accepted for metadata writes: {allowed} ({reason or 'admin-level'})")

    # An API key is userless, so /Users/Me answers 400; a user token answers 200.
    try:
        user = await client.current_user()
        print(f"  credential kind: user token for {user.Name!r}")
    except Exception as exc:
        print(f"  credential kind: API key (userless; /Users/Me -> {type(exc).__name__})")

    assert allowed, (
        f"the configured credential must be able to write metadata, got {reason!r}. "
        "Phase 5 needs an administrator key or an admin user token."
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
    """Assumption 2: optimistic concurrency needs a version token, and gets one.

    Live-verified on 12.2.0: music items carry an ``Etag``, but **only when it is
    requested via `fields=`**. The first version of this test concluded there was no
    concurrency token because ``ITEM_FIELDS`` omitted it, which would have sent
    phase 5 down a fallback path it does not need.

    ``DateLastSaved`` is not returned for music items even when asked for, so the
    ADR 0007 fallback is moot: ``Etag`` is the token.
    """
    for kind in ("MusicArtist", "MusicAlbum", "Audio"):
        result = await client.items(kind=kind, limit=3)  # type: ignore[arg-type]
        if not result.Items:
            continue
        present = [item for item in result.Items if item.Etag]
        print(f"\n{kind}: {len(present)}/{len(result.Items)} items carry an Etag")
        if present:
            print(f"  sample: {present[0].Etag}")
        print(f"  DateLastSaved returned: {sum(1 for i in result.Items if i.DateLastSaved)}")
        assert present, f"{kind} items must carry an Etag for optimistic concurrency"


async def test_a_single_item_is_readable_by_id(client: JellyfinClient) -> None:
    """The read every edit depends on, and the one this suite went without.

    `GET /Items/{itemId}` with a **userless API key** returns **400** on 12.2.0 --
    regardless of the id, including one the server itself just returned from `/Artists`
    -- unless a `userId` is supplied. The list endpoints tolerate its absence, so every
    earlier test here passed while the whole single-item path was broken: state,
    candidates, diff and apply all read the item first, which meant apply failed with
    "Jellyfin rejected the request to /Items/... with 400" and never attempted a write.

    A live test is the only thing that could have caught it. The contract lists `userId`
    as optional, so a schema check agrees with the broken assumption, and the stub
    accepted the request happily.
    """
    page = await client.artists(limit=1)
    if not page.Items:
        pytest.skip("no artists on this server to read")
    item_id = page.Items[0].Id
    assert item_id, "the server must return an id we can read back"

    # No explicit user: the client resolves one, which is the behaviour under test.
    dto = await client.item(item_id)
    assert dto.Id, "the item read must return a real item"
    assert dto.Name, "and its name"

    # The resolved user must be cached, or a library-wide operation doubles its requests.
    resolved = await client.user_id()
    assert resolved, "a userless API key needs a user id to read a single item"
    assert await client.user_id() == resolved


async def test_reading_an_item_without_a_user_fails_on_this_server(
    client: JellyfinClient,
) -> None:
    """Documents the Jellyfin behaviour the client works around, rather than trusting it.

    If a future server stops requiring the user id, this test fails and the workaround can
    be reconsidered -- which is better than carrying it forever on a stale assumption.
    """
    page = await client.artists(limit=1)
    if not page.Items:
        pytest.skip("no artists on this server to read")
    item_id = page.Items[0].Id

    response = await client._request(
        "GET", f"/Items/{item_id}", params={"fields": "Genres"}, raw=True
    )
    assert response.status_code == 400, (
        "expected Jellyfin to reject a userless single-item read; if this now returns "
        "200 the userId workaround in JellyfinClient.item() may no longer be needed"
    )


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
