"""The app against a faithful Jellyfin and Last.fm, with no network.

These tests exist because of a pattern worth naming: **every bug that reached a user in
this project was found by talking to the real servers, and none by the test suite.** Each
integration test carried its own permissive stub, so the mocks agreed with the client's
assumptions rather than the servers' behaviour, and a mock that accepts everything cannot
fail.

The mocks in `tests/support/` reproduce what the servers actually do, including the parts
that cost real debugging time, and they *reject* what the servers reject. Each test below
names the live observation it encodes, so the reason a mock behaves oddly is recoverable
rather than folklore.

The strongest of them is the full-overwrite test. The faithful mock really does null every
writable field absent from the body, so a payload that dropped a field would visibly
destroy it here -- which a merging mock could never show.
"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from tests.conftest import requires_postgres
from tests.support.jellyfin import FakeJellyfin, album, artist
from tests.support.lastfm import FakeLastfm

from metaedit.adapters.jellyfin.client import ITEM_FIELDS, JellyfinClient
from metaedit.adapters.lastfm.client import LastfmClient
from metaedit.archive.reindex import reindex
from metaedit.archive.store import ArchiveStore
from metaedit.config import Settings
from metaedit.db.partitions import ensure_partitions
from metaedit.domain.snapshot import from_dto, to_payload
from metaedit.domain.writable import payload_fields
from metaedit.service.harvest import derive_query, harvest_item

pytestmark = [requires_postgres]

JELLYFIN_URL = "http://jellyfin.test:8096"
LASTFM_URL = "http://lastfm.test/2.0/"


def _settings(**overrides: object) -> Settings:
    fields: dict[str, object] = {
        "LOG_JSON": False,
        "JELLYFIN_URL": JELLYFIN_URL,
        "JELLYFIN_API_KEY": "test-admin-key",
        "LASTFM_API_KEY": "test-lastfm-key",
        "LASTFM_API_ROOT": LASTFM_URL,
        "ARCHIVE_ENABLED": True,
    }
    fields.update(overrides)
    return Settings(_env_file=None, **fields)  # type: ignore[arg-type]


def _jellyfin(fake: FakeJellyfin, settings: Settings) -> JellyfinClient:
    """A client wired to the fake, as an async context manager's inner client."""
    return JellyfinClient(
        settings, client=httpx.AsyncClient(transport=fake.transport(), timeout=5.0)
    )


def _lastfm(fake: FakeLastfm, settings: Settings, session: object) -> LastfmClient:
    return LastfmClient(
        settings,
        store=ArchiveStore(session, settings),  # type: ignore[arg-type]
        client=httpx.AsyncClient(transport=fake.transport(), timeout=5.0),
    )


# =========================================================== Jellyfin fidelity


async def test_a_single_item_reads_without_the_caller_naming_a_user() -> None:
    """Encodes: `GET /Items/{id}` is 400 for a userless key unless `userId` is given.

    Live-verified on 12.2.0, for every id including ones the server itself returned. This
    broke the entire edit path, because apply reads the item before writing, so the failure
    surfaced as a 400 on the write and no write was ever attempted.
    """
    fake = FakeJellyfin([artist("a1", "Radiohead", mbid="m1")])
    settings = _settings()
    async with _jellyfin(fake, settings) as client:
        dto = await client.item("a1")

    assert dto.Name == "Radiohead"
    # The client must have supplied a user, which is the whole compensation.
    reads = [r for r in fake.recording.requests if r.url.path == "/Items/a1"]
    assert reads, "the item must have been read"
    assert reads[0].url.params.get("userId"), "without a userId this is a 400"


async def test_the_user_id_is_resolved_once_for_many_reads() -> None:
    """Encodes: an API key is userless, so the user must be discovered -- but only once.

    Re-resolving per item would double the request count for a library-wide run.
    """
    fake = FakeJellyfin([artist("a1", "One"), artist("a2", "Two")])
    settings = _settings()
    async with _jellyfin(fake, settings) as client:
        await client.item("a1")
        await client.item("a2")

    assert fake.recording.count("/Users") == 1


async def test_artist_browsing_does_not_filter_the_artists_endpoint() -> None:
    """Encodes: `/Artists` returns *nothing* when given `includeItemTypes`.

    The filter looks harmless and empties the browse, so the client must not send it.
    """
    fake = FakeJellyfin([artist("a1", "Radiohead"), artist("a2", "Portishead")])
    settings = _settings()
    async with _jellyfin(fake, settings) as client:
        page = await client.artists(limit=10)

    assert page.TotalRecordCount == 2, "the browse must not be emptied by a filter"
    assert {item.Name for item in page.Items} == {"Radiohead", "Portishead"}
    for request in fake.recording.requests:
        assert "includeItemTypes" not in request.url.params, (
            "adding includeItemTypes to /Artists empties the result"
        )


async def test_library_ids_come_from_the_endpoint_that_has_them() -> None:
    """Encodes: `/Library/MediaFolders` omits `ItemId`; `/Library/VirtualFolders` has it."""
    fake = FakeJellyfin()
    settings = _settings()
    async with _jellyfin(fake, settings) as client:
        libraries = await client.music_libraries()

    assert len(libraries) == 1
    assert libraries[0].ItemId == "lib-music", (
        "MediaFolders returns ItemId: null, so a browse-scoped id would be missing"
    )


async def test_the_etag_is_requested_so_concurrency_works() -> None:
    """Encodes: an Etag is only returned when asked for via `fields=`.

    Without it the client saw `None` and concluded there was no version token at all,
    which would have made ADR 0007 look unimplementable.
    """
    fake = FakeJellyfin([artist("a1", "Radiohead")])
    settings = _settings()
    async with _jellyfin(fake, settings) as client:
        dto = await client.item("a1")

    assert dto.Etag, "the client must request Etag or optimistic concurrency is impossible"
    assert "Etag" in ITEM_FIELDS, "and it must be in the requested field set"


async def test_the_album_artist_is_read_from_the_scalar_spelling() -> None:
    """Encodes: the server sends `AlbumArtist` as a scalar for albums, often instead of an
    `AlbumArtists` array. Reading only the array left an album with no artist at all, which
    made it impossible to look up on Last.fm."""
    fake = FakeJellyfin([album("b1", "OK Computer", "Radiohead")])
    settings = _settings()
    async with _jellyfin(fake, settings) as client:
        dto = await client.item("b1")

    item = from_dto(dto.model_dump(), "MusicAlbum")
    assert derive_query(item)["artist"] == "Radiohead"


async def test_a_write_sends_every_writable_field() -> None:
    """Encodes the hazard the whole tool is shaped around.

    `POST /Items/{id}` is a **full overwrite**, and the fake really performs it: a key
    absent from the body becomes null. A merging mock could never show this, so this test
    is the reason the mock nulls.
    """
    fake = FakeJellyfin(
        [artist("a1", "Radiohead", genres=["Rock"], tags=["curated"], overview="kept")]
    )
    settings = _settings()
    async with _jellyfin(fake, settings) as client:
        dto = await client.item("a1")
        payload = to_payload(from_dto(dto.model_dump(), "MusicArtist"), {"Genres": ["Art Rock"]})
        await client.update_item("a1", payload)

    sent = fake.last_write()
    assert set(sent) == set(payload_fields("MusicArtist")), (
        "a short body would null every omitted field on the server"
    )
    # The fields nobody selected survived the overwrite, because they were carried through.
    stored = fake.stored("a1")
    assert stored["Tags"] == ["curated"]
    assert stored["Overview"] == "kept"
    assert stored["Genres"] == ["Art Rock"], "and the selected field did change"


async def test_a_partial_body_would_destroy_data() -> None:
    """The negative case, proving the previous test is not vacuous.

    Sending only the changed field really does null the rest -- so the assertion above is
    testing something that can fail.
    """
    fake = FakeJellyfin([artist("a1", "Radiohead", tags=["curated"], overview="kept")])
    settings = _settings()
    async with _jellyfin(fake, settings) as client:
        await client.update_item("a1", {"Genres": ["Art Rock"]})

    stored = fake.stored("a1")
    assert stored["Tags"] is None, "an omitted field is nulled by a real full overwrite"
    assert stored["Overview"] is None
    assert stored["Name"] is None


async def test_a_rejected_write_reports_the_reason() -> None:
    """Encodes: a bare status is undiagnosable, and Jellyfin's 400s name the bad field."""
    fake = FakeJellyfin(
        [artist("a1", "Radiohead")],
        fail_writes_with=400,
        write_error_body={"title": "Bad Request", "detail": "PremiereDate was not recognised"},
    )
    settings = _settings()
    async with _jellyfin(fake, settings) as client:
        with pytest.raises(Exception) as caught:
            await client.update_item("a1", {})

    assert "PremiereDate was not recognised" in str(caught.value)


async def test_the_credential_is_confirmed_without_treating_a_userless_400_as_failure() -> None:
    """Encodes: `GET /Users/Me` is **400** for an API key, which is a success signal.

    API keys are userless and carry administrator privileges, so treating that 400 as an
    error made every API-key deployment look unable to write.
    """
    fake = FakeJellyfin()
    settings = _settings()
    async with _jellyfin(fake, settings) as client:
        allowed, reason = await client.can_write_metadata()

    assert allowed is True
    assert reason is None


# ============================================================= Last.fm fidelity


def _session_factory(database_url: str):  # type: ignore[no-untyped-def]
    """A session on the disposable test database, not the developer's one.

    Using the app's global engine made these tests read the archive I had filled by hand
    from the live API, so the fakes were never consulted. A test that can be satisfied by
    leftover state is not testing the thing it names.
    """
    engine = create_async_engine(database_url)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _prepared(database_url: str):  # type: ignore[no-untyped-def]
    """Engine and factory for a database that can actually accept an archive write.

    Migrations create the partitioned tables but not the partitions, so an insert fails
    with a CheckViolation until they exist.
    """
    engine, factory = _session_factory(database_url)
    async with factory() as session:
        await ensure_partitions(await session.connection(), months_ahead=1)
        await session.commit()
    return engine, factory


@requires_postgres
async def test_tag_popularity_comes_from_the_envelope_free_endpoint(database_url: str) -> None:
    """Encodes: `getTopTags` has no entity envelope, and `getInfo` tags carry **no counts**.

    So popularity can only come from the top-tags call. A mock that wrapped it, or that put
    counts in `getInfo`, would hide the bug class that made every tag count null.
    """
    fake = FakeLastfm()
    fake.add_artist(
        "Radiohead",
        mbid="m1",
        tags=["rock", "alternative"],
        top_tags=[("alternative rock", 100), ("rock", 89)],
    )
    settings = _settings()
    engine, factory = await _prepared(database_url)

    async with factory() as session:
        client = _lastfm(fake, settings, session)
        async with client:
            outcome = await harvest_item(
                session=session,
                client=client,
                item=from_dto(artist("a1", "Radiohead", mbid="m1"), "MusicArtist"),
            )
        await session.commit()
        await reindex(session)
        await session.commit()
    await engine.dispose()

    assert outcome.found is True
    assert "artist.gettoptags" in outcome.methods
    assert outcome.response_ids, "the responses must be archived, not just read"


@requires_postgres
async def test_album_counts_survive_lastfm_disagreeing_with_itself(database_url: str) -> None:
    """Encodes the punctuation inconsistency between Last.fm's own endpoints.

    For one real album `album.getInfo` returned a U+2026 ellipsis while `album.getTopTags`
    returned three full stops. Joined strictly, the counts were dropped and the album
    showed popularity for none of its tags -- 0 of 5 instead of 4 of 5.
    """
    fake = FakeLastfm(info_ellipsis="\u2026", top_tags_ellipsis="...")
    fake.add_album(
        "Metallica",
        "and Justice for All",
        tags=["thrash metal"],
        top_tags=[("thrash metal", 100), ("heavy metal", 80)],
    )
    settings = _settings()
    engine, factory = await _prepared(database_url)

    async with factory() as session:
        client = _lastfm(fake, settings, session)
        async with client:
            await harvest_item(
                session=session,
                client=client,
                item=from_dto(album("b1", "and Justice for All", "Metallica"), "MusicAlbum"),
            )
        await session.commit()
        report = await reindex(session)
        await session.commit()

    from sqlalchemy import text

    async with engine.connect() as conn:
        counted = await conn.scalar(
            text(
                "select count(*) from lastfm_tag_edge "
                "where entity_kind='album' and count is not null"
            )
        )
        value = await conn.scalar(
            text("select count from lastfm_tag_edge where entity_kind='album' limit 1")
        )
    await engine.dispose()
    assert value == 100, "and the count itself must be the real one"

    assert report.as_dict()["counts"]["unexpected_shapes"] == 0, (
        "an envelope-free tag response must not be flagged as an unreadable shape"
    )
    assert counted == 1, (
        "counts must survive the two endpoints spelling the album differently; the tag "
        "list comes from the envelope, so one tag means one countable edge"
    )


@requires_postgres
async def test_string_tag_counts_are_understood(database_url: str) -> None:
    """Encodes: the XML-derived responses send counts as strings, the JSON ones as ints.

    Refusing the string form would silently reduce those responses to "no popularity data".
    """
    fake = FakeLastfm(string_counts=True)
    fake.add_artist("Radiohead", tags=["rock"], top_tags=[("rock", 100)])
    settings = _settings()
    engine, factory = await _prepared(database_url)

    async with factory() as session:
        client = _lastfm(fake, settings, session)
        async with client:
            await harvest_item(
                session=session,
                client=client,
                item=from_dto(artist("a1", "Radiohead"), "MusicArtist"),
            )
        await session.commit()
        await reindex(session)
        await session.commit()

    from sqlalchemy import text

    async with engine.connect() as conn:
        counted = await conn.scalar(
            text("select count(*) from lastfm_tag_edge where count is not null")
        )
    await engine.dispose()
    assert counted == 1, "a numeric string is a count, not a missing value"


@requires_postgres
async def test_a_not_found_is_an_outcome_not_an_exception(database_url: str) -> None:
    """Encodes: Last.fm reports errors as a code in the body, 6 meaning not-found.

    A library-wide fetch must not abort on the first item Last.fm does not have.
    """
    fake = FakeLastfm()  # no artists registered, so every lookup misses
    settings = _settings()
    engine, factory = await _prepared(database_url)

    async with factory() as session:
        client = _lastfm(fake, settings, session)
        async with client:
            outcome = await harvest_item(
                session=session,
                client=client,
                item=from_dto(artist("a1", "Nobody At All"), "MusicArtist"),
                search_fallback=False,
            )
        await session.commit()
    await engine.dispose()

    assert outcome.found is False
    assert outcome.error_code == "not_found"
    assert outcome.error, "a miss must say why"


@requires_postgres
async def test_a_rejected_key_is_fatal_rather_than_a_miss(database_url: str) -> None:
    """Encodes: error 10 is a rejected credential, which no retry will fix.

    Reporting it as "not found" would send the operator looking at their library instead of
    their API key.
    """
    fake = FakeLastfm(api_key="a-different-key")
    settings = _settings()
    engine, factory = await _prepared(database_url)

    from metaedit.domain.errors import LastfmAuthError

    async with factory() as session:
        client = _lastfm(fake, settings, session)
        async with client:
            with pytest.raises(LastfmAuthError):
                await harvest_item(
                    session=session,
                    client=client,
                    item=from_dto(artist("a1", "Radiohead"), "MusicArtist"),
                )
    await engine.dispose()
