"""Applying and reverting edits — the only path that writes to Jellyfin.

Tested against a stub that records every write, because the assertions that matter
are about the *body*: whether an unselected field was carried through or nulled, and
whether a snapshot existed before the write happened. A real server would accept a
destructive body just as happily as a correct one.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from tests.conftest import requires_postgres

from metaedit.archive.reindex import reindex
from metaedit.archive.store import ArchiveStore, Observation
from metaedit.config import Settings, get_settings
from metaedit.db.models import AuditLog, Snapshot
from metaedit.db.partitions import ensure_partitions
from metaedit.db.session import get_session
from metaedit.domain.writable import payload_field_set
from metaedit.main import create_app

pytestmark = requires_postgres

BASE = "http://jellyfin.test:8096"
ITEM_ID = "aaaaaaaa-0000-0000-0000-000000000001"
BIO = "Radiohead are an English rock band formed in 1985 in Abingdon, Oxfordshire."

ARTIST_ENTITY = {
    "artist": {
        "name": "Radiohead",
        "mbid": "a74b1b7f-71a5-4011-9441-d0b5e4122711",
        "url": "https://www.last.fm/music/Radiohead",
        "stats": {"listeners": "5000000"},
        "tags": {
            "tag": [{"name": "art rock", "count": "100"}, {"name": "electronic", "count": "80"}]
        },
        "bio": {"summary": BIO, "published": "Thu, 13 Mar 2008"},
    }
}

ITEM: dict[str, Any] = {
    "Id": ITEM_ID,
    "Type": "MusicArtist",
    "Name": "Radiohead",
    "Etag": "etag-1",
    "SourceType": "Library",
    "Genres": ["Rock"],
    "Tags": ["curated-tag"],
    "ProviderIds": {},
    "ExternalUrls": [],
    "LockedFields": [],
    "Overview": "",
    "People": [],
    "Studios": [],
    "ProductionLocations": [],
}


class StubJellyfin:
    """A Jellyfin that records writes instead of performing them."""

    def __init__(self) -> None:
        self.writes: list[dict[str, Any]] = []
        self.item: dict[str, Any] = json.loads(json.dumps(ITEM))
        # When set, EVERY write fails, so the client's deliberate retry policy cannot
        # turn a failure path into a success.
        self.fail_writes_with: int | None = None

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from metaedit.adapters.jellyfin.client import JellyfinClient

        stub = self

        async def patched_enter(client: JellyfinClient) -> JellyfinClient:
            # Named `client`, not `self`: a parameter called `self` here would shadow
            # the stub and send `_handle` looking on the wrong object.
            client._client = httpx.AsyncClient(
                transport=httpx.MockTransport(stub._handle), timeout=5.0
            )
            client._owns_client = True
            return client

        monkeypatch.setattr(JellyfinClient, "__aenter__", patched_enter)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/Users/Me":
            return httpx.Response(200, json={"Id": "admin", "Policy": {"IsAdministrator": True}})
        if path == "/System/Info":
            return httpx.Response(200, json={"Version": "12.2.0"})
        if path.startswith("/Items/") and request.method == "POST":
            body = json.loads(request.content or b"{}")
            if self.fail_writes_with is not None:
                return httpx.Response(self.fail_writes_with, text="upstream failure")
            self.writes.append(body)
            # The server stores what it was sent, so a follow-up read reflects it.
            self.item = {**self.item, **body}
            return httpx.Response(204)
        if path == f"/Items/{ITEM_ID}":
            return httpx.Response(200, json=self.item)
        return httpx.Response(404)


def _settings(database_url: str) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        DATABASE_URL=database_url,
        JELLYFIN_URL=BASE,
        JELLYFIN_API_KEY="test-admin-key",
        ARCHIVE_ENABLED=True,
        LOG_JSON=False,
    )


def _seed(database_url: str, settings: Settings) -> None:
    engine = create_async_engine(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def run() -> None:
        async with factory() as session:
            await ensure_partitions(await session.connection(), months_ahead=1)
            store = ArchiveStore(session, settings)
            await store.record(
                Observation(
                    method="artist.getinfo",
                    params={"artist": "Radiohead", "autocorrect": "1"},
                    http_status=200,
                    duration_ms=5,
                    body=ARTIST_ENTITY,
                    user_agent="test",
                )
            )
            await session.commit()
            await reindex(session)
            await session.commit()

    asyncio.run(run())
    asyncio.run(engine.dispose())


@pytest.fixture
def stub() -> StubJellyfin:
    return StubJellyfin()


@pytest.fixture
def client(
    database_url: str, stub: StubJellyfin, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[TestClient, StubJellyfin]]:
    settings = _settings(database_url)
    _seed(database_url, settings)
    stub.install(monkeypatch)

    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings

    async def override_session():  # type: ignore[no-untyped-def]
        engine = create_async_engine(database_url)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise
        await engine.dispose()

    app.dependency_overrides[get_session] = override_session
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client, stub


def _entity_id(client: TestClient) -> int:
    body = client.post(f"/api/items/{ITEM_ID}/candidates", json={}).json()
    assert body["candidates"], f"no archived candidate resolved: {body}"
    return int(body["candidates"][0]["entity_id"])


def _query(database_url: str, statement: Any) -> Any:
    engine = create_async_engine(database_url)

    async def run() -> Any:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            return await session.scalar(statement)

    try:
        return asyncio.run(run())
    finally:
        asyncio.run(engine.dispose())


# ------------------------------------------------------------------ candidates


def test_candidates_resolve_from_the_archive(client: tuple[TestClient, StubJellyfin]) -> None:
    http, _ = client
    body = http.post(f"/api/items/{ITEM_ID}/candidates", json={}).json()
    assert body["count"] == 1
    candidate = body["candidates"][0]
    assert candidate["name"] == "Radiohead"
    assert candidate["matched_on"] == "name", "the item has no MBID yet"
    assert candidate["confidence"]["verdict"] == "review", "no ids on the item side"
    assert "nothing was written" in body["note"]


def test_candidates_do_not_write(client: tuple[TestClient, StubJellyfin]) -> None:
    http, stub = client
    http.post(f"/api/items/{ITEM_ID}/candidates", json={})
    assert stub.writes == []


# ------------------------------------------------------------------------ diff


def test_diff_proposes_changes_and_writes_nothing(client: tuple[TestClient, StubJellyfin]) -> None:
    http, stub = client
    entity_id = _entity_id(http)
    body = http.post(f"/api/items/{ITEM_ID}/diff", json={"entity_id": entity_id}).json()

    proposed = {change["field"] for change in body["changes"]}
    assert "Genres" in proposed
    assert "Overview" in proposed
    assert body["default_selection"] == [], "a review-verdict match pre-selects nothing"
    assert stub.writes == [], "a diff must never write"


def test_diff_explains_withheld_fields(client: tuple[TestClient, StubJellyfin]) -> None:
    http, _ = client
    entity_id = _entity_id(http)
    body = http.post(f"/api/items/{ITEM_ID}/diff", json={"entity_id": entity_id}).json()
    withheld = {change["field"]: change["withheld_reason"] for change in body["withheld"]}
    assert withheld, "some fields must be withheld for this item"
    assert all(reason for reason in withheld.values())


def test_diff_rejects_an_unknown_entity(client: tuple[TestClient, StubJellyfin]) -> None:
    http, _ = client
    response = http.post(f"/api/items/{ITEM_ID}/diff", json={"entity_id": 999999})
    assert response.status_code == 404


# ----------------------------------------------------------------------- apply


def test_apply_requires_confirmation(client: tuple[TestClient, StubJellyfin]) -> None:
    """A write is never the default outcome of a request."""
    http, stub = client
    entity_id = _entity_id(http)
    response = http.post(
        f"/api/items/{ITEM_ID}/apply",
        json={"entity_id": entity_id, "fields": ["Genres"]},
    )
    assert response.status_code == 422
    assert "confirm" in response.json()["error"]["message"]
    assert stub.writes == []


def test_apply_writes_a_complete_payload(client: tuple[TestClient, StubJellyfin]) -> None:
    """The property that stops this tool destroying what it does not touch."""
    http, stub = client
    entity_id = _entity_id(http)
    response = http.post(
        f"/api/items/{ITEM_ID}/apply",
        json={"entity_id": entity_id, "fields": ["Genres"], "confirm": True},
    )
    assert response.status_code == 200, response.text
    assert len(stub.writes) == 1

    sent = stub.writes[0]
    assert set(sent) == payload_field_set("MusicArtist"), (
        "a short body would null every omitted field on the server"
    )


def test_apply_preserves_unselected_fields(client: tuple[TestClient, StubJellyfin]) -> None:
    http, stub = client
    entity_id = _entity_id(http)
    http.post(
        f"/api/items/{ITEM_ID}/apply",
        json={"entity_id": entity_id, "fields": ["Genres"], "confirm": True},
    )
    sent = stub.writes[0]
    assert sent["Name"] == "Radiohead"
    assert sent["Tags"] == ["curated-tag"], "an unselected field must survive"
    assert sent["Overview"] == "", "and must not be silently filled either"
    assert sent["ProviderIds"] == {}


def test_apply_merges_genres_rather_than_replacing(client: tuple[TestClient, StubJellyfin]) -> None:
    http, stub = client
    entity_id = _entity_id(http)
    http.post(
        f"/api/items/{ITEM_ID}/apply",
        json={"entity_id": entity_id, "fields": ["Genres"], "confirm": True},
    )
    genres = stub.writes[0]["Genres"]
    assert "Rock" in genres, "the curated genre must survive"
    assert "art rock" in genres


def test_apply_snapshots_before_writing(client: tuple[TestClient, StubJellyfin]) -> None:
    http, _stub = client
    entity_id = _entity_id(http)
    response = http.post(
        f"/api/items/{ITEM_ID}/apply",
        json={"entity_id": entity_id, "fields": ["Genres"], "confirm": True},
    ).json()

    snapshot_id = response["snapshot_id"]
    assert snapshot_id is not None
    assert response["applied"] == ["Genres"]
    assert response["etag_before"] == "etag-1"


def test_apply_records_an_audit_entry_with_provenance(
    client: tuple[TestClient, StubJellyfin], database_url: str
) -> None:
    http, _ = client
    entity_id = _entity_id(http)
    http.post(
        f"/api/items/{ITEM_ID}/apply",
        json={"entity_id": entity_id, "fields": ["Genres"], "confirm": True},
    )
    engine = create_async_engine(database_url)

    async def run() -> tuple[Any, ...]:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            row = (
                await session.execute(select(AuditLog).order_by(AuditLog.id.desc()).limit(1))
            ).scalar_one()
            return row.action, row.outcome, row.changed_fields, row.lastfm_candidate

    action, outcome, changed, candidate = asyncio.run(run())
    asyncio.run(engine.dispose())
    assert action == "apply"
    assert outcome == "applied"
    assert changed == ["Genres"]
    assert candidate["response_id"], "the written value traces back to a stored body"


def test_apply_refuses_a_stale_etag_without_writing(
    client: tuple[TestClient, StubJellyfin], stub: StubJellyfin
) -> None:
    """Optimistic concurrency: a stale read must not silently revert someone's edit."""
    http, _ = client
    entity_id = _entity_id(http)
    response = http.post(
        f"/api/items/{ITEM_ID}/apply",
        json={
            "entity_id": entity_id,
            "fields": ["Genres"],
            "confirm": True,
            "expected_etag": "etag-from-yesterday",
        },
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conflict"
    assert stub.writes == [], "nothing may be written when the item has moved on"


def test_apply_rejects_a_field_that_was_not_proposed(
    client: tuple[TestClient, StubJellyfin], stub: StubJellyfin
) -> None:
    """A field the caller can name but we did not plan is one they could destroy."""
    http, _ = client
    entity_id = _entity_id(http)
    response = http.post(
        f"/api/items/{ITEM_ID}/apply",
        json={"entity_id": entity_id, "fields": ["Name"], "confirm": True},
    )
    assert response.status_code == 422
    assert "not proposed" in response.json()["error"]["message"]
    assert stub.writes == []


def test_apply_refuses_a_locked_field(client: tuple[TestClient, StubJellyfin]) -> None:
    http, stub = client
    stub.item = {**stub.item, "LockedFields": ["Genres"], "Etag": "etag-locked"}
    entity_id = _entity_id(http)
    response = http.post(
        f"/api/items/{ITEM_ID}/apply",
        json={"entity_id": entity_id, "fields": ["Genres"], "confirm": True},
    )
    assert response.status_code == 422
    assert "locked" in response.json()["error"]["message"].lower()
    assert stub.writes == []


def test_an_empty_selection_writes_the_current_values(
    client: tuple[TestClient, StubJellyfin],
) -> None:
    """Selecting nothing is a legitimate no-op, and must not be a destructive one."""
    http, stub = client
    entity_id = _entity_id(http)
    response = http.post(
        f"/api/items/{ITEM_ID}/apply",
        json={"entity_id": entity_id, "fields": [], "confirm": True},
    )
    assert response.status_code == 200
    sent = stub.writes[0]
    assert sent["Genres"] == ["Rock"]
    assert sent["Tags"] == ["curated-tag"]
    assert response.json()["applied"] == []


def test_a_failed_write_is_audited_and_leaves_the_snapshot(
    client: tuple[TestClient, StubJellyfin], stub: StubJellyfin, database_url: str
) -> None:
    """A failed write must be recorded, and the snapshot is harmless if it was not needed.

    The reverse ordering -- write first, snapshot after -- would make a failure leave
    an unrecoverable edit.
    """
    http, _ = client
    entity_id = _entity_id(http)
    stub.fail_writes_with = 503
    response = http.post(
        f"/api/items/{ITEM_ID}/apply",
        json={"entity_id": entity_id, "fields": ["Genres"], "confirm": True},
    )
    assert response.status_code >= 400

    async def run() -> tuple[int, int, str | None]:
        engine = create_async_engine(database_url)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            snapshots = await session.scalar(select(func.count()).select_from(Snapshot))
            audit = (
                await session.execute(select(AuditLog).order_by(AuditLog.id.desc()).limit(1))
            ).scalar_one()
            return int(snapshots or 0), int(audit.id), audit.outcome

    snapshots, _, outcome = asyncio.run(run())
    assert snapshots >= 1, "the snapshot is written before the write attempt"
    assert outcome == "failed", "the attempt is recorded rather than lost"


# ---------------------------------------------------------------------- revert


def _apply_then_revert(
    http: TestClient, stub: StubJellyfin
) -> tuple[dict[str, Any], dict[str, Any]]:
    entity_id = _entity_id(http)
    applied = http.post(
        f"/api/items/{ITEM_ID}/apply",
        json={"entity_id": entity_id, "fields": ["Genres", "Overview"], "confirm": True},
    ).json()
    reverted = http.post(
        f"/api/snapshots/{applied['snapshot_id']}/revert", params={"confirm": "true"}
    ).json()
    return applied, reverted


def test_revert_restores_the_previous_values(client: tuple[TestClient, StubJellyfin]) -> None:
    http, stub = client
    _, reverted = _apply_then_revert(http, stub)
    assert reverted["applied"], "a revert must actually change something back"
    # The last write is the revert; it must carry the original values.
    sent = stub.writes[-1]
    assert sent["Genres"] == ["Rock"], "the original genre list is restored"
    assert sent["Overview"] == "", "an empty overview is restored as empty"


def test_revert_requires_confirmation(client: tuple[TestClient, StubJellyfin]) -> None:
    http, stub = client
    entity_id = _entity_id(http)
    applied = http.post(
        f"/api/items/{ITEM_ID}/apply",
        json={"entity_id": entity_id, "fields": ["Genres"], "confirm": True},
    ).json()
    writes_before = len(stub.writes)
    response = http.post(f"/api/snapshots/{applied['snapshot_id']}/revert")
    assert response.status_code == 422
    assert len(stub.writes) == writes_before


def test_a_revert_is_itself_snapshotted(client: tuple[TestClient, StubJellyfin]) -> None:
    """So undoing an undo works, and history is never mutated."""
    http, stub = client
    applied, reverted = _apply_then_revert(http, stub)
    assert reverted["snapshot_id"] not in {None, applied["snapshot_id"]}

    listing = http.get(f"/api/items/{ITEM_ID}/snapshots").json()
    operations = [entry["source_op"] for entry in listing["snapshots"]]
    assert "apply" in operations
    assert "revert" in operations
    assert len(operations) >= 2


def test_revert_writes_a_complete_payload(client: tuple[TestClient, StubJellyfin]) -> None:
    http, stub = client
    _apply_then_revert(http, stub)
    assert set(stub.writes[-1]) == payload_field_set("MusicArtist")


def test_revert_of_an_unknown_snapshot_is_a_404(client: tuple[TestClient, StubJellyfin]) -> None:
    http, _ = client
    response = http.post("/api/snapshots/999999/revert", params={"confirm": "true"})
    assert response.status_code == 404


def test_snapshot_history_is_newest_first(client: tuple[TestClient, StubJellyfin]) -> None:
    http, stub = client
    _apply_then_revert(http, stub)
    listing = http.get(f"/api/items/{ITEM_ID}/snapshots").json()
    ids = [entry["id"] for entry in listing["snapshots"]]
    assert ids == sorted(ids, reverse=True)
