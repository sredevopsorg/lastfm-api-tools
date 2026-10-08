"""Bulk editing.

The properties that matter here are not "it processes many items" but the failure
behaviour: a batch must not be applicable without review, one item failing must not
abandon the rest, and the whole run must be revertible as one unit. Those are what
make a library-wide operation recoverable.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from tests.conftest import requires_postgres

from metaedit.archive.reindex import reindex
from metaedit.archive.store import ArchiveStore, Observation
from metaedit.config import Settings, get_settings
from metaedit.db.models import Snapshot
from metaedit.db.partitions import ensure_partitions
from metaedit.db.session import get_session
from metaedit.main import create_app
from metaedit.service import bulk

pytestmark = requires_postgres

BASE = "http://jellyfin.test:8096"

# id-a and id-b match by MusicBrainz id (confidence 1.0). id-d has an archived entity
# but no id on the item, so it can only be matched by name and scores 0.833 -- which is
# what makes the confidence threshold observable. id-c has no archive entry at all.
ARTISTS = [
    ("id-a", "Radiohead", "a74b1b7f-71a5-4011-9441-d0b5e4122711"),
    ("id-b", "Portishead", "8f6bd1e4-fbe1-4f50-aa9b-94c450ec0f11"),
    ("id-c", "Nobody At All", None),
]
NAME_ONLY = ("id-d", "Massive Attack")


def _entity(name: str, mbid: str) -> dict[str, Any]:
    return {
        "artist": {
            "name": name,
            "mbid": mbid,
            "url": f"https://www.last.fm/music/{name.replace(' ', '+')}",
            "tags": {
                "tag": [{"name": "trip hop", "count": "90"}, {"name": "seen live", "count": "80"}]
            },
            "bio": {"summary": f"{name} are a band with a sufficiently long biography."},
        }
    }


def _item(item_id: str, name: str, mbid: str | None) -> dict[str, Any]:
    return {
        "Id": item_id,
        "Type": "MusicArtist",
        "Name": name,
        "Etag": f"etag-{item_id}",
        "SourceType": "Library",
        "Genres": [],
        "Tags": [],
        "ProviderIds": {"MusicBrainzArtist": mbid} if mbid else {},
        "ExternalUrls": [],
        "LockedFields": [],
        "Overview": "",
        "People": [],
        "Studios": [],
        "ProductionLocations": [],
    }


class StubJellyfin:
    """A Jellyfin holding several artists, recording every write."""

    def __init__(self) -> None:
        self.items = {item_id: _item(item_id, name, mbid) for item_id, name, mbid in ARTISTS}
        self.items[NAME_ONLY[0]] = _item(NAME_ONLY[0], NAME_ONLY[1], None)
        self.writes: list[tuple[str, dict[str, Any]]] = []
        # Item ids whose writes should fail, to exercise per-item isolation.
        self.fail_for: set[str] = set()

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from metaedit.adapters.jellyfin.client import JellyfinClient

        stub = self

        async def patched_enter(client: JellyfinClient) -> JellyfinClient:
            client._client = httpx.AsyncClient(
                transport=httpx.MockTransport(stub._handle), timeout=5.0
            )
            client._owns_client = True
            return client

        monkeypatch.setattr(JellyfinClient, "__aenter__", patched_enter)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/Users/Me":
            return httpx.Response(200, json={"Id": "a", "Policy": {"IsAdministrator": True}})
        if path == "/System/Info":
            return httpx.Response(200, json={"Version": "12.2.0"})
        if path == "/Items":
            raw_ids = request.url.params.get("ids")
            if raw_ids:
                found = [self.items[i] for i in raw_ids.split(",") if i in self.items]
            else:
                # A browse query (includeItemTypes), not an id lookup.
                found = list(self.items.values())
                limit = request.url.params.get("limit")
                if limit and str(limit).isdigit():
                    found = found[: int(limit)]
            return httpx.Response(200, json={"Items": found, "TotalRecordCount": len(found)})
        if path.startswith("/Items/") and request.method == "POST":
            item_id = path.split("/")[2]
            if item_id in self.fail_for:
                return httpx.Response(500, text="boom")
            body = json.loads(request.content or b"{}")
            self.writes.append((item_id, body))
            self.items[item_id] = {**self.items[item_id], **body}
            return httpx.Response(204)
        if path.startswith("/Items/"):
            item_id = path.split("/")[2]
            if item_id in self.items:
                return httpx.Response(200, json=self.items[item_id])
            return httpx.Response(404)
        return httpx.Response(404)


def _settings(database_url: str) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        DATABASE_URL=database_url,
        JELLYFIN_URL=BASE,
        JELLYFIN_API_KEY="test-key",
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
            for name, mbid in [
                *[(name, mbid) for _, name, mbid in ARTISTS if mbid],
                (NAME_ONLY[1], "f0e1d2c3-b4a5-4968-8778-99aabbccddee"),
            ]:
                await store.record(
                    Observation(
                        method="artist.getinfo",
                        params={"artist": name, "autocorrect": "1"},
                        http_status=200,
                        duration_ms=5,
                        body=_entity(name, mbid),
                        user_agent="test",
                    )
                )
            await session.commit()
            await reindex(session)
            await session.commit()

    asyncio.run(run())
    asyncio.run(engine.dispose())


def _events(text: str) -> list[dict[str, Any]]:
    """Parse an SSE body into its JSON payloads."""
    events: list[dict[str, Any]] = []
    for block in text.split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data: "):
                events.append(json.loads(line[len("data: ") :]))
    return events


@pytest.fixture
def stub() -> StubJellyfin:
    bulk.registry().clear()
    return StubJellyfin()


@pytest.fixture
def client(
    database_url: str, stub: StubJellyfin, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[TestClient, StubJellyfin, str]]:
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
        yield test_client, stub, database_url
    bulk.registry().clear()


SELECTION = {"selection": {"kind": "artist", "limit": 10}}


# ------------------------------------------------------------------------ diff


def test_bulk_diff_streams_every_item_and_writes_nothing(
    client: tuple[TestClient, StubJellyfin, str],
) -> None:
    http, stub, _ = client
    response = http.post("/api/bulk/diff", json=SELECTION)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")

    events = _events(response.text)
    items = [event for event in events if event["type"] == "item"]
    assert len(items) == 4, "every selected item is reported, including unusable ones"
    assert stub.writes == [], "a diff must never write"
    assert events[-1]["type"] == "done"


def test_bulk_diff_reports_the_job_id_for_a_later_apply(
    client: tuple[TestClient, StubJellyfin, str],
) -> None:
    http, _, _ = client
    events = _events(http.post("/api/bulk/diff", json=SELECTION).text)
    summary = next(event for event in events if event["type"] == "summary")
    assert summary["job_id"]
    assert summary["batch_id"]
    assert summary["applicable"] == 2, "only the id-matched items are actionable"
    assert summary["skipped"] == 2, "no-candidate and name-only are both reported"


def test_an_item_without_an_archived_candidate_is_skipped_with_a_reason(
    client: tuple[TestClient, StubJellyfin, str],
) -> None:
    """Silently dropping items would make a batch look complete when it was not."""
    http, _, _ = client
    events = _events(http.post("/api/bulk/diff", json=SELECTION).text)
    items = [event for event in events if event["type"] == "item"]
    skipped = [event for event in items if event["skipped_reason"]]
    assert len(skipped) == 1
    assert skipped[0]["name"] == "Nobody At All"
    assert "no archived" in skipped[0]["skipped_reason"].lower()
    assert skipped[0]["applicable"] is False


def test_min_confidence_skips_untrustworthy_matches(
    client: tuple[TestClient, StubJellyfin, str],
) -> None:
    """A batch must not contain guesses, so a threshold turns them into skips.

    The name-only match scores 0.833: above the review threshold but below auto, which
    is exactly the band this setting exists to exclude. The id-matched items score 1.0
    and stay actionable, so the threshold discriminates rather than blanket-excludes.
    """
    http, stub, _ = client
    body = {**SELECTION, "min_confidence": 0.9}
    events = _events(http.post("/api/bulk/diff", json=body).text)
    items = [event for event in events if event["type"] == "item"]
    name_only = next(event for event in items if event["name"] == "Massive Attack")
    assert name_only["applicable"] is False
    assert "below the" in name_only["skipped_reason"]

    summary = next(event for event in events if event["type"] == "summary")
    assert summary["applicable"] == 2, "the id-matched items remain actionable"
    assert stub.writes == []


def test_bulk_diff_rejects_an_oversized_batch(client: tuple[TestClient, StubJellyfin, str]) -> None:
    http, _, _ = client
    response = http.post("/api/bulk/diff", json={"selection": {"kind": "artist", "limit": 100000}})
    assert response.status_code == 422


# ----------------------------------------------------------------------- apply


def test_bulk_apply_requires_confirmation(client: tuple[TestClient, StubJellyfin, str]) -> None:
    http, stub, _ = client
    job_id = _job_id(http)
    response = http.post("/api/bulk/apply", json={"job_id": job_id})
    assert response.status_code == 422
    assert "confirm" in response.json()["error"]["message"]
    assert stub.writes == []


def test_bulk_apply_requires_a_diff_job(client: tuple[TestClient, StubJellyfin, str]) -> None:
    """A bulk write is never the first request of a session."""
    http, stub, _ = client
    response = http.post("/api/bulk/apply", json={"job_id": "never-existed", "confirm": True})
    assert response.status_code == 404
    assert stub.writes == []


def test_bulk_apply_writes_every_applicable_item(
    client: tuple[TestClient, StubJellyfin, str],
) -> None:
    http, stub, _ = client
    events = _events(
        http.post("/api/bulk/apply", json={"job_id": _job_id(http), "confirm": True}).text
    )
    applied = [event for event in events if event["type"] == "applied"]
    assert len(applied) == 2
    assert {item_id for item_id, _ in stub.writes} == {"id-a", "id-b"}
    summary = next(event for event in events if event["type"] == "summary")
    assert summary["applied"] == 2
    assert summary["skipped"] == 2, "no-candidate and name-only are both reported"
    assert summary["batch_revert"].endswith("/revert")


def test_every_write_in_a_batch_is_complete(client: tuple[TestClient, StubJellyfin, str]) -> None:
    """The full-overwrite guarantee must hold per item, not just for single edits."""
    from metaedit.domain.writable import payload_field_set

    http, stub, _ = client
    http.post("/api/bulk/apply", json={"job_id": _job_id(http), "confirm": True})
    assert stub.writes
    for item_id, body in stub.writes:
        assert set(body) == payload_field_set("MusicArtist"), item_id


def test_one_item_failing_does_not_abandon_the_batch(
    client: tuple[TestClient, StubJellyfin, str],
) -> None:
    """The property that makes a library-wide run recoverable.

    A failure must be isolated so the operator can see exactly where it stopped, and
    the items already written must still be revertible.
    """
    http, stub, _ = client
    stub.fail_for = {"id-a"}
    events = _events(
        http.post("/api/bulk/apply", json={"job_id": _job_id(http), "confirm": True}).text
    )

    failed = [event for event in events if event["type"] == "failed"]
    applied = [event for event in events if event["type"] == "applied"]
    assert [event["item_id"] for event in failed] == ["id-a"]
    assert [event["item_id"] for event in applied] == ["id-b"], "the later item still runs"

    summary = next(event for event in events if event["type"] == "summary")
    assert summary["failed"] == 1
    assert summary["applied"] == 1
    assert summary["failures"][0]["item_id"] == "id-a"


def test_an_unexpected_failure_is_contained_to_its_own_item(
    client: tuple[TestClient, StubJellyfin, str],
) -> None:
    """A bug on one item must not become a bug on the whole batch.

    The existing test above covers a failure we anticipated (an upstream error).
    This one covers a failure we did not: an arbitrary exception raised while
    snapshotting the first item. Catching only `MetaeditError` left the session
    dirty, so the *next* item's flush raised `PendingRollbackError` and the batch
    died with the original cause buried underneath -- observed live, on a
    `TypeError` from a datetime reaching a JSONB column.
    """
    http, _stub, _ = client
    job_id = _job_id(http)

    calls = {"n": 0}
    real = bulk.apply_plan

    async def flaky(**kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise TypeError("Object of type datetime is not JSON serializable")
        return await real(**kwargs)

    with patch.object(bulk, "apply_plan", flaky):
        events = _events(
            http.post("/api/bulk/apply", json={"job_id": job_id, "confirm": True}).text
        )

    assert calls["n"] > 1, "the batch kept going after the unexpected failure"
    failed = [event for event in events if event["type"] == "failed"]
    applied = [event for event in events if event["type"] == "applied"]
    assert len(failed) == 1
    assert failed[0]["error_code"] == "internal_error"
    # The reference id, not the exception text: a TypeError's str() describes us.
    assert "datetime" not in failed[0]["error"]
    assert "Reference" in failed[0]["error"] or "reference" in failed[0]["error"]
    assert applied, "later items still ran"

    summary = next(event for event in events if event["type"] == "summary")
    assert summary["failed"] == 1
    assert summary["applied"] == len(applied)


def test_a_job_cannot_be_applied_twice(client: tuple[TestClient, StubJellyfin, str]) -> None:
    """Re-applying a reviewed batch would duplicate writes nobody reviewed again."""
    http, stub, _ = client
    job_id = _job_id(http)
    http.post("/api/bulk/apply", json={"job_id": job_id, "confirm": True})
    writes_after_first = len(stub.writes)

    events = _events(http.post("/api/bulk/apply", json={"job_id": job_id, "confirm": True}).text)
    error = next(event for event in events if event["type"] == "error")
    assert "already been applied" in error["message"]
    assert len(stub.writes) == writes_after_first


def test_bulk_apply_can_write_a_reviewed_field_subset(
    client: tuple[TestClient, StubJellyfin, str],
) -> None:
    http, stub, _ = client
    events = _events(
        http.post(
            "/api/bulk/apply",
            json={"job_id": _job_id(http), "fields": ["Genres"], "confirm": True},
        ).text
    )
    assert [event for event in events if event["type"] == "applied"]
    for _, body in stub.writes:
        assert body["Genres"], "the requested field was written"
        assert body["Overview"] == "", "and the unrequested one was left alone"


# ---------------------------------------------------------------------- revert


def _apply_batch(http: TestClient) -> str:
    events = _events(
        http.post("/api/bulk/apply", json={"job_id": _job_id(http), "confirm": True}).text
    )
    summary = next(event for event in events if event["type"] == "summary")
    return str(summary["batch_id"])


def test_batch_revert_requires_confirmation(
    client: tuple[TestClient, StubJellyfin, str],
) -> None:
    """Refused before the stream starts, so the client gets a real status code."""
    http, stub, _ = client
    batch_id = _apply_batch(http)
    stub.writes.clear()
    response = http.post(f"/api/bulk/{batch_id}/revert")
    assert response.status_code == 422
    assert "confirm" in response.json()["error"]["message"]
    assert stub.writes == []


def test_batch_revert_restores_every_item(client: tuple[TestClient, StubJellyfin, str]) -> None:
    http, stub, _ = client
    batch_id = _apply_batch(http)
    stub.writes.clear()

    events = _events(http.post(f"/api/bulk/{batch_id}/revert", params={"confirm": "true"}).text)
    reverted = [event for event in events if event["type"] == "reverted"]
    assert len(reverted) == 2
    assert {item_id for item_id, _ in stub.writes} == {"id-a", "id-b"}
    for _, body in stub.writes:
        assert body["Genres"] == [], "the pre-batch genre list is restored"


def test_batch_revert_reports_an_unknown_batch(
    client: tuple[TestClient, StubJellyfin, str],
) -> None:
    http, _, _ = client
    events = _events(http.post("/api/bulk/does-not-exist/revert", params={"confirm": "true"}).text)
    assert any(event["type"] == "error" for event in events)


def test_batch_snapshots_are_committed_and_labelled(
    client: tuple[TestClient, StubJellyfin, str],
) -> None:
    """The batch id lives on the snapshots, which is why revert survives a restart."""
    http, _, database_url = client
    batch_id = _apply_batch(http)
    engine = create_async_engine(database_url)

    async def run() -> list[str]:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            rows = (
                await session.execute(select(Snapshot).where(Snapshot.batch_id == batch_id))
            ).scalars()
            return [row.source_op for row in rows]

    operations = asyncio.run(run())
    asyncio.run(engine.dispose())
    assert operations == ["apply", "apply"]


def test_jobs_endpoint_lists_a_reviewed_batch(client: tuple[TestClient, StubJellyfin, str]) -> None:
    """An operator should be able to see what is queued before applying it."""
    http, _, _ = client
    job_id = _job_id(http)
    body = http.get("/api/bulk/jobs").json()
    assert body["count"] == 1
    assert body["jobs"][0]["job_id"] == job_id
    assert body["jobs"][0]["applied"] is False


def _job_id(http: TestClient) -> str:
    events = _events(http.post("/api/bulk/diff", json=SELECTION).text)
    summary = next(event for event in events if event["type"] == "summary")
    return str(summary["job_id"])
