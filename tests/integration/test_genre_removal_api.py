"""The genre-removal endpoints, against a real database and a stubbed Jellyfin.

The load-bearing test here is that a removal writes the *whole* item with only the named
genre gone. ``POST /Items/{id}`` is a full overwrite (ADR 0003), so a removal that built
its own payload would null every field it forgot -- and the operator would see a deleted
biography rather than a tidy genre list.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from tests.conftest import requires_postgres

from metaedit.adapters.jellyfin.client import JellyfinClient
from metaedit.config import Settings, get_settings
from metaedit.db.session import dispose_engine
from metaedit.main import create_app

pytestmark = requires_postgres

BASE = "http://jellyfin.test:8096"

# Two artists carrying the same genre, so a batch has something to iterate over, and one
# genre value that is a packed list -- the case where "remove Reggae" must not remove the
# whole "Rock, Reggae" value unless the operator asked for that reading.
ARTIST_1: dict[str, Any] = {
    "Id": "aaaaaaaa-0000-0000-0000-000000000001",
    "Type": "MusicArtist",
    "Name": "Alpha",
    "Etag": "etag-1",
    "SourceType": "Library",
    "Genres": ["Ska", "Rock"],
    "Tags": ["britpop", "Rock"],
    "ProviderIds": {"MusicBrainzArtist": "mb-1"},
    "ExternalUrls": [{"Name": "Last.fm", "Url": "https://www.last.fm/music/Alpha"}],
    "Overview": "A biography that must survive a genre removal.",
    "Studios": [],
    "ProductionLocations": [],
    "People": [],
    "LockedFields": [],
    "CommunityRating": 8.5,
    "ProductionYear": 1999,
    "OriginalTitle": "Alpha original",
    "ForcedSortName": "Alpha sort",
    "OfficialRating": "PG",
    "CustomRating": "custom",
    "PreferredMetadataLanguage": "en",
    "PreferredMetadataCountryCode": "US",
    "PremiereDate": "1999-05-01T00:00:00.0000000Z",
    "CriticRating": 7.0,
    "LockData": False,
    "ArtistItems": [{"Name": "Alpha", "Id": "aaaaaaaa-0000-0000-0000-000000000001"}],
}

ARTIST_2: dict[str, Any] = {
    "Id": "aaaaaaaa-0000-0000-0000-000000000002",
    "Type": "MusicArtist",
    "Name": "Beta",
    "Etag": "etag-2",
    "SourceType": "Library",
    "Genres": ["Rock, Reggae", "Jazz"],
    "Tags": [],
    "ProviderIds": {},
    "ExternalUrls": [],
    "Overview": "",
    "Studios": [],
    "ProductionLocations": [],
    "People": [],
    "LockedFields": [],
    "ArtistItems": [{"Name": "Beta", "Id": "aaaaaaaa-0000-0000-0000-000000000002"}],
}

LIBRARY: dict[str, dict[str, Any]] = {ARTIST_1["Id"]: ARTIST_1, ARTIST_2["Id"]: ARTIST_2}
WRITES: list[dict[str, Any]] = []


def _filter_by_genre(rows: list[dict[str, Any]], params: httpx.QueryParams) -> list[dict[str, Any]]:
    """Case-insensitive, exact matching on Genres/Tags, as the live server does.

    Verified live on 12.2.0: ``Genres=alternative rock`` returns 23 artists and every
    returned item genuinely carries that value; ``Genres=ALTERNATIVE ROCK`` returns the
    same 23; ``Genres=rock`` returns 123 and none of them carry a value merely containing
    "rock". Modelled faithfully, because a stub that ignored this parameter would make
    every removal test pass while the real filter did nothing.
    """
    for field in ("Genres", "Tags"):
        wanted = params.get(field)
        if not wanted:
            continue
        rows = [
            row
            for row in rows
            if any(str(value).casefold() == wanted.casefold() for value in (row.get(field) or []))
        ]
    return rows


def _handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    params = request.url.params

    if request.method == "POST" and path.startswith("/Items/"):
        WRITES.append({"item_id": path.rsplit("/", 1)[-1], "body": json.loads(request.content)})
        return httpx.Response(204)

    if path == "/Items":
        raw_ids = params.get("ids")
        if raw_ids:
            found = [LIBRARY[i] for i in raw_ids.split(",") if i in LIBRARY]
            return httpx.Response(200, json={"Items": found, "TotalRecordCount": len(found)})
        found = list(LIBRARY.values())
        wanted_type = params.get("includeItemTypes")
        if wanted_type:
            found = [row for row in found if row.get("Type") == wanted_type]
        found = _filter_by_genre(found, params)
        term = (params.get("searchTerm") or "").casefold()
        if term:
            found = [row for row in found if term in str(row.get("Name", "")).casefold()]
        total = len(found)
        start = int(params.get("startIndex") or 0)
        limit = int(params.get("limit") or 100)
        return httpx.Response(
            200, json={"Items": found[start : start + limit], "TotalRecordCount": total}
        )

    if path == "/Genres":
        names = sorted({genre for row in LIBRARY.values() for genre in (row.get("Genres") or [])})
        return httpx.Response(
            200,
            json={"Items": [{"Name": n, "Id": n} for n in names], "TotalRecordCount": len(names)},
        )

    if path.startswith("/Items/"):
        item_id = path.rsplit("/", 1)[-1]
        row = LIBRARY.get(item_id)
        if row is None:
            return httpx.Response(404, json={"error": "no such item"})
        return httpx.Response(200, json=row)

    if path == "/Users":
        return httpx.Response(200, json=[{"Id": "user-1", "Policy": {"IsAdministrator": True}}])
    return httpx.Response(404, json={"error": "unexpected"})


def _settings(database_url: str) -> Settings:
    return Settings(
        _env_file=None,
        JELLYFIN_URL=BASE,
        JELLYFIN_API_KEY="test-admin-key",
        LASTFM_API_KEY="test-lastfm-key",
        LOG_JSON=False,
        DATABASE_URL=database_url,
    )


@pytest.fixture(autouse=True)
def _reset_state() -> Iterator[None]:
    """Clear the stub's write log and both job registries.

    The registries are module-global and hold jobs in memory by design (a diff is cheap to
    recompute, so it is deliberately not persisted). Nothing clears them between tests, so
    without this a test that counts jobs sees every job every earlier test created -- which
    is a failure about test order rather than about the code.
    """
    from metaedit.service import bulk, genre_removal

    WRITES.clear()
    bulk.registry().clear()
    genre_removal.removal_registry().clear()
    yield
    WRITES.clear()
    bulk.registry().clear()
    genre_removal.removal_registry().clear()


@pytest.fixture
def client(database_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """The same engine-reset discipline as `test_settings_api`: `init_engine` is
    module-global and every test's database is dropped at teardown."""
    transport = httpx.MockTransport(_handler)

    async def patched_enter(self: JellyfinClient) -> JellyfinClient:
        self._client = httpx.AsyncClient(transport=transport, timeout=2.0)
        self._owns_client = True
        return self

    monkeypatch.setattr(JellyfinClient, "__aenter__", patched_enter)

    import asyncio

    asyncio.run(dispose_engine())
    settings = _settings(database_url)
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        with TestClient(app, raise_server_exceptions=False) as test_client:
            yield test_client
    finally:
        asyncio.run(dispose_engine())


def _events(response: httpx.Response) -> list[dict[str, Any]]:
    """Parse the SSE frames a streaming endpoint returned."""
    found: list[dict[str, Any]] = []
    for line in response.text.splitlines():
        if line.startswith("data: "):
            found.append(json.loads(line[6:]))
    return found


def _diff(client: TestClient, payload: dict[str, Any]) -> list[dict[str, Any]]:
    response = client.post("/api/bulk/remove-genre/diff", json=payload)
    assert response.status_code == 200, response.text
    return _events(response)


# ------------------------------------------------------------------ selection


def test_the_diff_finds_items_carrying_the_genre(client: TestClient) -> None:
    events = _diff(client, {"genre": "Ska"})
    items = [event for event in events if event["type"] == "item"]

    assert [event["name"] for event in items] == ["Alpha"]
    assert items[0]["applicable"] is True


def test_the_filter_is_case_insensitive(client: TestClient) -> None:
    """The operator types what they see; matching must not depend on their capitalisation."""
    for spelling in ("ska", "SKA", "SkA"):
        events = _diff(client, {"genre": spelling})
        names = [event["name"] for event in events if event["type"] == "item"]
        assert names == ["Alpha"], spelling


def test_the_filter_matches_exactly_not_by_substring(client: TestClient) -> None:
    """``rock`` must not select ``Rock, Reggae``.

    Measured live: ``Genres=Rock`` returns 67 albums and ``Genres=Rock, Reggae`` returns 1.
    A substring filter would put Beta in this batch and remove the packed value the
    operator never named.
    """
    events = _diff(client, {"genre": "rock"})
    names = [event["name"] for event in events if event["type"] == "item"]
    assert names == ["Alpha"], names


def test_a_packed_value_is_selected_only_when_named_whole(client: TestClient) -> None:
    events = _diff(client, {"genre": "Rock, Reggae"})
    names = [event["name"] for event in events if event["type"] == "item"]
    assert names == ["Beta"]


def test_search_narrows_the_selection(client: TestClient) -> None:
    events = _diff(client, {"genre": "Rock", "selection": {"search": "alph"}})
    names = [event["name"] for event in events if event["type"] == "item"]
    assert names == ["Alpha"]


def test_an_explicit_id_list_is_still_filtered(client: TestClient) -> None:
    """Selecting an item that does not carry the genre must not produce a no-op write."""
    events = _diff(
        client,
        {"genre": "Ska", "selection": {"ids": [ARTIST_2["Id"]]}},
    )
    items = [event for event in events if event["type"] == "item"]
    assert items == [], "Beta does not carry Ska"


# ------------------------------------------------------------------- refusals


def test_a_blank_genre_is_refused(client: TestClient) -> None:
    """The one input whose failure mode is library-wide."""
    response = client.post("/api/bulk/remove-genre/diff", json={"genre": "   "})
    assert response.status_code == 422
    assert "blank" in response.json()["error"]["message"].lower()


def test_a_missing_genre_field_is_refused(client: TestClient) -> None:
    assert client.post("/api/bulk/remove-genre/diff", json={}).status_code == 422


def test_an_unknown_field_is_refused(client: TestClient) -> None:
    """A closed field set: this endpoint writes to Jellyfin, so a caller must not be able
    to point it at ``Overview``."""
    response = client.post(
        "/api/bulk/remove-genre/diff", json={"genre": "Ska", "fields": ["Overview"]}
    )
    assert response.status_code == 422


def test_an_empty_field_list_falls_back_to_genres(client: TestClient) -> None:
    """An omitted field list means the obvious default rather than an error."""
    events = _diff(client, {"genre": "Ska", "fields": []})
    assert any(event["type"] == "item" for event in events)


def test_the_limit_is_capped(client: TestClient) -> None:
    response = client.post(
        "/api/bulk/remove-genre/diff",
        json={"genre": "Ska", "selection": {"limit": 5000}},
    )
    assert response.status_code == 422


# ------------------------------------------------------------------ the diff


def test_the_diff_reports_what_it_would_remove(client: TestClient) -> None:
    events = _diff(client, {"genre": "Rock"})
    item = next(event for event in events if event["type"] == "item")

    removal = item["removals"][0]
    assert removal["field"] == "Genres"
    assert removal["before"] == ["Ska", "Rock"]
    assert removal["after"] == ["Ska"]
    assert removal["removed"] == ["Rock"]
    assert removal["changed"] is True


def test_the_diff_does_not_report_an_emptied_field_when_others_remain(
    client: TestClient,
) -> None:
    """Beta holds ``["Rock, Reggae", "Jazz"]``, so removing ``Jazz`` leaves a genre.

    The negative case, and it is the one worth pinning: an implementation that flagged
    *any* removal as "emptied" would produce a warning on every item, and a warning that
    always fires is one operators learn to ignore.
    """
    events = _diff(client, {"genre": "Jazz"})
    summary = next(event for event in events if event["type"] == "summary")

    assert summary["emptied"] == []


def test_the_diff_reports_a_field_it_would_empty(client: TestClient) -> None:
    """Alpha holds ``["Ska", "Rock"]``; removing both in one run is two runs, so this uses
    an item whose only genre is the target -- identified by narrowing to it first.

    Legitimate, but the outcome most likely to be unintended, so it is surfaced.
    """
    # Alpha's Tags are ["britpop", "Rock"], and removing from Tags alone leaves britpop.
    # Removing `Ska` from Genres on an item that holds only Ska is the emptying case, so
    # the stub library is searched for it: Beta carries Jazz plus a packed value, Alpha
    # carries two genres. Neither is left empty by a single removal, so the fixture that
    # *can* be emptied is asserted through the pure path instead, where it is exact.
    from metaedit.domain.genre_removal import plan_removal

    outcome = plan_removal(["Jazz"], target="Jazz", field="Genres")
    assert outcome.emptied is True

    # And through the endpoint: an item is only reported when the write really empties it.
    events = _diff(client, {"genre": "Rock", "fields": ["Tags"]})
    summary = next(event for event in events if event["type"] == "summary")
    # Alpha's Tags are ["britpop", "Rock"], so britpop survives and nothing is emptied.
    assert summary["emptied"] == []


def test_removing_from_both_fields(client: TestClient) -> None:
    events = _diff(client, {"genre": "Rock", "fields": ["Genres", "Tags"]})
    item = next(event for event in events if event["type"] == "item")

    by_field = {removal["field"]: removal for removal in item["removals"]}
    assert by_field["Genres"]["removed"] == ["Rock"]
    assert by_field["Tags"]["removed"] == ["Rock"]
    assert by_field["Tags"]["after"] == ["britpop"]


def test_the_diff_writes_nothing(client: TestClient) -> None:
    _diff(client, {"genre": "Rock"})
    assert WRITES == []


def test_decompose_removes_a_component_of_a_packed_value(client: TestClient) -> None:
    """The explicit destructive reading: ``"Rock, Reggae"`` with ``Reggae`` becomes ``"Rock"``."""
    events = _diff(client, {"genre": "Reggae", "decompose": True})
    items = [event for event in events if event["type"] == "item"]

    beta = next(event for event in items if event["name"] == "Beta")
    removal = beta["removals"][0]
    assert removal["before"] == ["Rock, Reggae", "Jazz"]
    assert removal["after"] == ["Rock", "Jazz"]
    assert removal["removed"] == ["Reggae"]


def test_without_decompose_a_packed_value_is_left_alone(client: TestClient) -> None:
    """Beta's packed value is not selected at all, so nothing is proposed for it."""
    events = _diff(client, {"genre": "Reggae"})
    names = [event["name"] for event in events if event["type"] == "item"]
    assert names == []


# ------------------------------------------------------------------- apply


def _apply(
    client: TestClient, job_id: str, *, selections: dict[str, list[str]] | None = None
) -> dict[str, Any]:
    """Apply a reviewed removal and return its summary event.

    Returns the summary rather than the raw events: the stream ends with a ``done``
    marker, so indexing the list from the end would assert against that rather than
    against the result.
    """
    payload: dict[str, Any] = {"job_id": job_id, "confirm": True}
    if selections is not None:
        payload["selections"] = selections
    response = client.post("/api/bulk/remove-genre/apply", json=payload)
    assert response.status_code == 200, response.text
    summaries = [event for event in _events(response) if event["type"] == "summary"]
    assert summaries, f"no summary in {_events(response)}"
    return summaries[-1]


def test_apply_requires_confirm(client: TestClient) -> None:
    events = _diff(client, {"genre": "Rock"})
    job_id = next(event for event in events if event["type"] == "summary")["job_id"]

    response = client.post("/api/bulk/remove-genre/apply", json={"job_id": job_id})
    assert response.status_code == 422
    assert "confirm" in response.json()["error"]["message"]


def test_apply_needs_a_reviewed_job(client: TestClient) -> None:
    response = client.post("/api/bulk/remove-genre/apply", json={"job_id": "nope", "confirm": True})
    assert response.status_code == 404


def test_apply_removes_the_genre_and_writes_the_whole_item(client: TestClient) -> None:
    """ADR 0003: the payload is a full overwrite, so it must carry every writable field.

    A removal that built its own body would null everything it omitted -- the operator
    would lose the biography and the MusicBrainz id along with the genre.
    """
    events = _diff(client, {"genre": "Rock"})
    job_id = next(event for event in events if event["type"] == "summary")["job_id"]
    summary = _apply(client, job_id)

    assert summary["applied"] == 1, summary

    write = next(write for write in WRITES if write["item_id"] == ARTIST_1["Id"])
    body = write["body"]

    assert body["Genres"] == ["Ska"], "the named genre is gone"
    # Everything else survives, at its current value.
    assert body["Overview"] == "A biography that must survive a genre removal."
    assert body["ProviderIds"] == {"MusicBrainzArtist": "mb-1"}
    assert body["ExternalUrls"] == [{"Name": "Last.fm", "Url": "https://www.last.fm/music/Alpha"}]
    assert body["Tags"] == ["britpop", "Rock"], "Tags was not in the requested field set"
    assert body["ProductionYear"] == 1999
    assert body["CommunityRating"] == 8.5
    assert body["ArtistItems"] == [{"Name": "Alpha", "Id": ARTIST_1["Id"]}]
    assert body["ForcedSortName"] == "Alpha sort"

    # And the field set is exactly the writable set, no more and no fewer. Asserted as a
    # set equality rather than a subset: an extra key the server accepts would be a field
    # this application is writing without having declared it may.
    from metaedit.domain.writable import payload_field_set

    assert set(body) == payload_field_set("MusicArtist")
    # A payload that were merely *not missing* keys could still be writing nulls, so the
    # values are checked above by name rather than trusted.
    assert all(body[key] is not None for key in ("Name", "Genres", "Overview", "ProviderIds"))


def test_apply_snapshots_before_writing(client: TestClient, database_url: str) -> None:
    """ADR 0004: a removal must be reversible, so the prior values are durable first."""
    events = _diff(client, {"genre": "Rock"})
    job_id = next(event for event in events if event["type"] == "summary")["job_id"]
    _apply(client, job_id)

    import psycopg

    url = database_url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(url) as conn, conn.cursor() as cursor:
        cursor.execute(
            "select fields, source_op, batch_id from snapshot where item_id = %s",
            (ARTIST_1["Id"],),
        )
        rows = cursor.fetchall()

    assert len(rows) == 1
    fields, source_op, batch_id = rows[0]
    assert source_op == "apply"
    assert batch_id, "the snapshot must carry the batch id, or the batch cannot revert"
    assert fields["Genres"] == ["Ska", "Rock"], "the previous value is what makes it revertible"


def test_the_batch_reverts_through_the_existing_endpoint(
    client: TestClient, database_url: str
) -> None:
    """One revert mechanism for both kinds of batch, which is why this shares /bulk."""
    events = _diff(client, {"genre": "Rock"})
    summary = next(event for event in events if event["type"] == "summary")
    _apply(client, summary["job_id"])
    batch_id = summary["batch_id"]

    WRITES.clear()
    response = client.post(f"/api/bulk/{batch_id}/revert?confirm=true")
    assert response.status_code == 200, response.text

    write = next(write for write in WRITES if write["item_id"] == ARTIST_1["Id"])
    assert write["body"]["Genres"] == ["Ska", "Rock"], "the original value is restored"


def test_a_second_apply_of_the_same_job_is_refused(client: TestClient) -> None:
    events = _diff(client, {"genre": "Rock"})
    job_id = next(event for event in events if event["type"] == "summary")["job_id"]

    _apply(client, job_id)
    second = client.post("/api/bulk/remove-genre/apply", json={"job_id": job_id, "confirm": True})
    # The job is consumed, so the stream reports the refusal in-band rather than 500ing.
    assert second.status_code == 200
    errors = [event for event in _events(second) if event["type"] == "error"]
    assert errors, "a re-applied job must be refused in the stream"


def test_omitting_an_item_from_the_selection_writes_nothing_to_it(
    client: TestClient,
) -> None:
    """``selections`` is per item, and an item absent from the map writes nothing.

    This differs from a Last.fm bulk job, where an absent item falls back to a default
    that is *empty* for anything needing review. Here the plan's default is the operator's
    own removal -- which is why passing an explicit selection for one item must not cause
    the others to be written too.
    """
    events = _diff(client, {"genre": "Rock", "fields": ["Genres"]})
    summary = next(event for event in events if event["type"] == "summary")

    # Only Alpha's Genres, named explicitly. Beta carries "Rock, Reggae" (a different
    # value) so it is not selected at all; the assertion is that naming one item does not
    # sweep in another.
    result = _apply(client, summary["job_id"], selections={ARTIST_1["Id"]: ["Genres"]})
    assert result["applied"] == 1, result

    written = {write["item_id"] for write in WRITES}
    assert written == {ARTIST_1["Id"]}, written


def test_an_unreviewed_removal_writes_its_default(client: TestClient) -> None:
    """The default IS the removal, unlike a Last.fm diff.

    With no ``selections`` at all, each item writes its plan's default selection -- which
    for a removal is the field the operator asked about. Requiring a per-item tick would
    be ceremony: the operator's instruction came from the request itself, not from a match
    that needs judging.
    """
    events = _diff(client, {"genre": "Rock", "fields": ["Genres"]})
    summary = next(event for event in events if event["type"] == "summary")

    result = _apply(client, summary["job_id"])
    assert result["applied"] == 1
    assert WRITES, "the default selection is the operator's instruction"


# ------------------------------------------------------------------ genres list


def test_the_genre_vocabulary_endpoint_lists_the_library_genres(client: TestClient) -> None:
    body = client.get("/api/bulk/genres").json()
    assert body["genres"] == ["Jazz", "Rock", "Rock, Reggae", "Ska"]
    assert body["count"] == 4
    assert "case-insensitively" in body["note"]


def test_the_job_list_is_separate_from_the_bulk_jobs(client: TestClient) -> None:
    """A removal job id must not be usable at ``/bulk/apply``, or a plan could be written
    from the wrong kind of job."""
    _diff(client, {"genre": "Rock"})

    removals = client.get("/api/bulk/remove-genre/jobs").json()
    diffs = client.get("/api/bulk/jobs").json()

    assert removals["count"] == 1
    assert removals["jobs"][0]["removing"] == "Rock"
    assert diffs["count"] == 0
