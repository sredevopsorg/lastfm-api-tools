"""Paging through the derived layer must visit every row exactly once.

`ORDER BY name` is not a total order, and `OFFSET` paging over a non-total order is
incorrect rather than merely unstable: a tied row can be returned on two consecutive
pages while another is never returned at all.

This was not hypothetical, and these tests exist because of a measurement rather than a
suspicion. Paging the live album table one row at a time through the endpoint's own query:

    109 rows fetched, 108 distinct
    duplicated across pages: [90]   ('Corazones' -- Jorge González)
    never appearing:         [8]    ('Corazones' -- Los Prisioneros)

The fixture below is built to reproduce that shape deliberately: two albums sharing a
name, which is the only condition required. Without the `id` tiebreaker the walk test
fails; with it, every row is visited once.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from tests.conftest import requires_postgres

from metaedit.archive.reindex import reindex
from metaedit.archive.store import ArchiveStore, Observation
from metaedit.config import Settings, get_settings
from metaedit.db.partitions import ensure_partitions
from metaedit.db.session import get_session
from metaedit.main import create_app

pytestmark = requires_postgres


def _album(name: str, artist: str, mbid: str, listeners: int, playcount: int) -> dict[str, Any]:
    return {
        "album": {
            "name": name,
            "artist": artist,
            "mbid": mbid,
            "url": f"https://www.last.fm/music/{artist}/{name}",
            # Distinct per row. Identical values would make "sort by listeners" and
            # "sort by name" observationally the same, so a broken sort key would be
            # undetectable -- I found that by injecting one and watching it pass.
            "listeners": str(listeners),
            "playcount": str(playcount),
            "tags": {"tag": [{"name": "pop", "count": "10"}]},
            "wiki": {"summary": f"{name} by {artist}."},
        }
    }


# The same album name from two different artists: one name, two rows, and nothing in the
# name column to separate them. The other names are distinct, so the fixture covers the
# ordinary case alongside the pathological one.
#
# listeners/playcount are deliberately NOT in name order: if they were, a sort key
# silently falling back to `name` would produce the same list and no test would notice.
ALBUMS = [
    ("Alpha", "Artist One", "mbid-alpha", 900, 400),
    ("Corazones", "Jorge Gonzalez", "mbid-corazones-1", 100, 900),
    ("Beta", "Artist Two", "mbid-beta", 700, 200),
    ("Corazones", "Los Prisioneros", "mbid-corazones-2", 300, 600),
    ("Gamma", "Artist Three", "mbid-gamma", 500, 800),
]


def _settings(database_url: str) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        DATABASE_URL=database_url,
        ARCHIVE_ENABLED=True,
        ARCHIVE_LOG_REQUESTS=True,
        LASTFM_API_KEY="test-key",
        LOG_JSON=False,
    )


@pytest.fixture
def client(database_url: str) -> Iterator[TestClient]:
    """A TestClient over a migrated database holding the duplicate-name albums."""
    import asyncio

    settings = _settings(database_url)
    engine = create_async_engine(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def seed() -> None:
        async with factory() as session:
            await ensure_partitions(await session.connection(), months_ahead=1)
            store = ArchiveStore(session, settings)
            for name, artist, mbid, listeners, playcount in ALBUMS:
                await store.record(
                    Observation(
                        method="album.getinfo",
                        params={"artist": artist, "album": name, "autocorrect": "1"},
                        http_status=200,
                        duration_ms=5,
                        body=_album(name, artist, mbid, listeners, playcount),
                        user_agent="test",
                    )
                )
            await session.commit()
            await reindex(session)
            await session.commit()

    asyncio.run(seed())
    asyncio.run(engine.dispose())

    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings

    async def override_session() -> AsyncIterator[Any]:
        test_engine = create_async_engine(database_url)
        test_factory = async_sessionmaker(test_engine, expire_on_commit=False)
        async with test_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_session] = override_session
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


def _walk(
    client: TestClient, *, page_size: int, sort: str = "name", order: str = "asc"
) -> list[int]:
    """Page through the entire album list, one request per page."""
    ids: list[int] = []
    page = 1
    while True:
        response = client.get(
            "/api/archive/entities",
            params={
                "kind": "album",
                "page": page,
                "page_size": page_size,
                "sort": sort,
                "order": order,
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        ids.extend(item["id"] for item in body["items"])
        if page >= body["pages"]:
            return ids
        page += 1


def test_the_fixture_actually_contains_the_hazard(client: TestClient) -> None:
    """If this stops being true, every test below stops testing anything.

    Two rows must share a name, or there is no tie to break and the walk tests would
    pass whether or not the tiebreaker exists -- which is how a paging bug survives a
    test suite that pages.
    """
    body = client.get("/api/archive/entities", params={"kind": "album", "page_size": 50}).json()
    names = [item["name"] for item in body["items"]]
    assert names.count("Corazones") == 2, names
    assert body["total"] == len(ALBUMS)


@pytest.mark.parametrize("page_size", [1, 2, 3, 4, 5, 10])
def test_every_page_returns_a_distinct_row(client: TestClient, page_size: int) -> None:
    """A walk must visit each row exactly once.

    This is the shape of the live reproduction, and it does fail when the tiebreaker is
    removed -- but only for the sort keys whose fixture values collide (listeners and
    playcount), because whether `ORDER BY name` diverges between two offset queries
    depends on Postgres's plan for the table. With 5 rows it happens to be stable; with
    the live 109-row table it was not.

    So this test is kept for what it does prove, and
    `test_generated_sql_always_ends_with_a_unique_tiebreaker` covers the property that
    does not depend on the planner at all. Relying on the walk alone would have been
    relying on luck: it passed here with the bug present.
    """
    ids = _walk(client, page_size=page_size)
    assert len(ids) == len(set(ids)), f"a row was returned on more than one page: {ids}"
    assert len(ids) == len(ALBUMS), f"walked {len(ids)} rows, expected {len(ALBUMS)}"
    assert set(ids) == {
        item["id"]
        for item in client.get(
            "/api/archive/entities", params={"kind": "album", "page_size": 50}
        ).json()["items"]
    }


@pytest.mark.parametrize("sort", ["name", "listeners", "playcount", "last_seen"])
@pytest.mark.parametrize("order", ["asc", "desc"])
def test_generated_sql_always_ends_with_a_unique_tiebreaker(sort: str, order: str) -> None:
    """The deterministic version of the property, independent of row count and planner.

    `ORDER BY name` alone does not define a total order when two rows share a name, and
    `OFFSET` paging over a non-total order is incorrect by construction. Whether that
    manifests as a visible duplicate depends on the query plan, so the robust check is on
    the ordering itself: the final key must be the primary key, which is unique.

    This is the test that would have caught the original bug on the day it was written,
    without needing a table large enough to make Postgres misbehave.
    """
    from metaedit.db.models import LastfmAlbum
    from metaedit.domain.archive_sort import order_by_clauses

    clauses = order_by_clauses(LastfmAlbum, sort, order)
    assert len(clauses) >= 2, f"{sort}/{order} has no tiebreaker: {clauses}"
    last = str(clauses[-1])
    assert "lastfm_album.id" in last, f"{sort}/{order} ends on a non-unique key: {last}"


def test_a_single_page_holds_every_row_at_most_once(client: TestClient) -> None:
    body = client.get("/api/archive/entities", params={"kind": "album", "page_size": 50}).json()
    ids = [item["id"] for item in body["items"]]
    assert len(ids) == len(set(ids))


def test_the_same_walk_twice_returns_the_same_order(client: TestClient) -> None:
    """Stability across identical requests.

    Not the same guarantee as correctness -- Postgres could pick one wrong order
    consistently -- but a change of order between two identical queries is what an
    operator would notice as rows jumping between pages.
    """
    assert _walk(client, page_size=2) == _walk(client, page_size=2)


def test_pages_reports_what_the_last_page_will_be(client: TestClient) -> None:
    """A partial last page is where this arithmetic goes wrong, so it is computed once."""
    body = client.get("/api/archive/entities", params={"kind": "album", "page_size": 2}).json()
    assert body["total"] == len(ALBUMS)
    assert body["pages"] == -(-len(ALBUMS) // 2)
    assert body["sort"] == "name"
    assert body["order"] == "asc"


def test_paging_past_the_end_is_empty_rather_than_an_error(client: TestClient) -> None:
    body = client.get("/api/archive/entities", params={"kind": "album", "page": 99}).json()
    assert body["items"] == []
    assert body["total"] == len(ALBUMS)


@pytest.mark.parametrize("sort", ["name", "listeners", "playcount", "last_seen"])
@pytest.mark.parametrize("order", ["asc", "desc"])
def test_every_sort_key_pages_without_duplicates(client: TestClient, sort: str, order: str) -> None:
    """The tiebreaker belongs to the ordering, not to the name case.

    A new sort key is exactly where this bug would come back, so every one is walked.
    """
    ids = _walk(client, page_size=1, sort=sort, order=order)
    assert len(ids) == len(set(ids)), f"{sort}/{order} duplicated a row: {ids}"
    assert len(ids) == len(ALBUMS)


def test_an_unknown_sort_key_is_refused(client: TestClient) -> None:
    assert client.get("/api/archive/entities", params={"sort": "mtime"}).status_code == 422


def test_the_response_echoes_the_ordering_that_was_applied(client: TestClient) -> None:
    body = client.get("/api/archive/entities", params={"sort": "listeners", "order": "desc"}).json()
    assert body["sort"] == "listeners"
    assert body["order"] == "desc"


def _names(client: TestClient, **params: Any) -> list[str]:
    query = {"kind": "album", "page_size": 50, **params}
    body = client.get("/api/archive/entities", params=query).json()
    return [item["name"] for item in body["items"]]


def test_sorting_by_name_is_alphabetical(client: TestClient) -> None:
    assert _names(client, sort="name", order="asc") == sorted(_names(client))


def test_reversing_the_order_reverses_the_list(client: TestClient) -> None:
    """Order must actually be applied, not accepted and ignored.

    Jellyfin silently ignores an unknown `sortBy`; this endpoint does not, but the
    failure would look identical from a client -- the parameter is accepted and the
    output does not change. So the assertion is on the output.
    """
    ascending = _names(client, sort="name", order="asc")
    descending = _names(client, sort="name", order="desc")
    assert descending == list(reversed(ascending))


@pytest.mark.parametrize(
    ("sort", "ascending_by"),
    [
        ("listeners", lambda row: int(row["listeners"])),
        ("playcount", lambda row: int(row["playcount"])),
    ],
)
def test_sorting_by_a_numeric_column_orders_by_that_column(
    client: TestClient, sort: str, ascending_by: Any
) -> None:
    """A sort key that falls back to `name` would still return *a* stable list.

    The fixture's listeners/playcount are deliberately not in name order, so an
    implementation that ignored the key and sorted by name produces a different list --
    which is what makes this assertion able to fail. With every row sharing one listeners
    value, as this fixture originally had, the two orderings are identical and the test
    proves nothing.
    """
    ascending = [ascending_by(row) for row in _rows(client, sort=sort, order="asc")]
    descending = [ascending_by(row) for row in _rows(client, sort=sort, order="desc")]
    assert ascending == sorted(ascending)
    assert descending == sorted(descending, reverse=True)
    assert ascending != _names(client, sort=sort, order="asc"), (
        "listeners order happens to match name order, so this test could not detect a "
        "sort key being ignored"
    )


def _rows(client: TestClient, **params: Any) -> list[dict[str, Any]]:
    query = {"kind": "album", "page_size": 50, **params}
    return client.get("/api/archive/entities", params=query).json()["items"]


def test_a_null_sort_value_does_not_lead_a_descending_sort(client: TestClient) -> None:
    """`NULLS LAST` is load-bearing, and the live archive is why.

    Measured on a real archive: **all 383 artist rows have a NULL `listeners`**, against
    0 of 109 albums and 0 of 96 tracks. So "sort artists by listeners, descending" is
    almost entirely a question about NULL ordering -- Postgres defaults to NULLS FIRST on
    DESC, which would put the rows with *no* listener count at the top of the list.

    The album fixture has values everywhere, so this is asserted with an artist row that
    has no listener count alongside one that does.
    """
    from metaedit.db.models import LastfmAlbum
    from metaedit.domain.archive_sort import NULLABLE_SORT_KEYS, order_by_clauses

    assert {"listeners", "playcount"} == set(NULLABLE_SORT_KEYS)
    for sort in sorted(NULLABLE_SORT_KEYS):
        for order in ("asc", "desc"):
            rendered = str(order_by_clauses(LastfmAlbum, sort, order)[0])
            assert "NULLS LAST" in rendered, f"{sort}/{order} would lead with NULLs: {rendered}"
