# metaedit

Browse a Jellyfin music library, read artist / album / track metadata from the
Last.fm API, keep a persistent local archive of every Last.fm response, review a
field-level diff, and apply the accepted changes back to Jellyfin.

- **Read-only toward Last.fm.** One API key, no user auth, no session signing.
  Nothing is ever written back to Last.fm.
- **Reviewed, reversible writes toward Jellyfin.** Every change is previewed
  field by field, and every apply is snapshotted so it can be undone.
- **A real archive, not a cache.** Every request and response is stored; derived
  data is rebuildable from the raw bytes with no network access.

## Status

Under construction, phase by phase. All eight phases are implemented and verified. See *Testing* for how to run each layer.

### Hardening

Security headers and a request-body cap are applied in the app rather than left to a
reverse proxy, because this is a self-hosted tool people put on their own LAN:

- A **restrictive CSP**. The SPA shares an origin with the write endpoints, so injected
  script could edit metadata. The policy costs nothing here — no third-party script, no
  inline script, no remote font — and the Last.fm image CDN is allowed for images only.
- `nosniff`, `frame-ancestors 'none'`, `Referrer-Policy: no-referrer`, a
  `Permissions-Policy` denying camera/microphone/geolocation.
- A **2 MiB body cap**, checked against the declared `Content-Length`, so a lying header
  does not get an unbounded read.
- The Jellyfin and Last.fm keys are `SecretStr`, so `model_dump()` and `repr()` cannot
  leak them, and a test asserts that. Another asserts no endpoint or log line echoes
  either key.

Not added on purpose: rate limiting of *our* API. The expensive operations are
operator-triggered and already bounded (a batch is capped at 500 items, each write costs
one Jellyfin request), and a limiter would mostly inconvenience the single user this tool
has. The Last.fm client does rate-limit, because that is a shared third-party service
with published limits.

### The UI

React + Vite single-page app, served by the same container as the API. Four screens:

| Screen | What it does |
| --- | --- |
| **Library** | Browse artists, albums or songs; filter to items missing metadata; open the editor. |
| **Editor** | Pick an archived candidate, review the diff field by field, write only what you tick, undo from the snapshot history. |
| **Bulk** | Review a selection, apply the reviewed job, watch per-item progress, revert the batch as a unit. |
| **Archive** | Storage-cap headroom, the derived layer's counts, stored entities with tags by popularity and similar artists. |

### End-to-end tests

Nine Playwright specs cover the critical journey through a real browser: browse, search,
review a diff, apply one field, undo it, revert a batch, and inspect the archive.

```bash
./scripts/e2e.sh          # run the suite
./scripts/e2e.sh --keep   # leave the stack up to inspect a failure
```

Every service it talks to is a stand-in, in a **separate compose file**
(`docker-compose.e2e.yml`) rather than a profile of the development one: the journey
*writes* metadata, and a flag that could point it at a real library is a flag somebody
will eventually set. The stub Jellyfin reproduces the real server's awkward behaviours —
MediaBrowser-only auth, userless API keys answering 400 on `/Users/Me`, `/Artists`
emptying when filtered, `/Library/MediaFolders` omitting `ItemId`, `Etag` only when
requested — so the app is exercised against the quirks rather than a convenient fiction.

The assertion that matters most is on the **server's** state, not the UI's message.
`POST /Items/{id}` is a full overwrite, so a body that omits a field nulls it; checking
only what the app says it did would pass even if it had destroyed the item. Verified by
reintroducing exactly that bug: three specs fail, including the one that asserts
unselected fields survive.

The SPA's types are **generated** from the backend's OpenAPI document rather than
hand-written, and a CI step fails if a schema change was not regenerated. Query
parameters are typed too, which is not decoration: the library page once sent `limit`
to an endpoint that takes `page_size`, so every page silently came back at the default
size. With the parameter types derived from the same document, that is now a compile
error with an actionable message.

| Area | State |
|---|---|
| Container, Postgres 18, migrations, health endpoints | **done** |
| CI: lint, types, tests, SPA build, image smoke test | **done** |
| Archive schema (raw + derived), byte accounting vs. the ToS cap | **done** |
| SPA shell served by the API | **done** |
| Jellyfin read: libraries, browse, batch item state, full write whitelist | **done** |
| Last.fm client: typed models, shared rate limiter, backoff, archive-first reads | **done** |
| Archive writes: append-only request log, content-addressed bodies, cap policy | **done** |
| Derivation into entity/tag/similarity tables, `reindex` | **done** |
| Tag policy, mapping, confidence, diff | **done** |
| Apply, snapshot, undo | **done** |
| Bulk editing (SSE) | **done** |
| Library browser, editor, bulk and archive-explorer UI | **done** |
| Hardening, end-to-end tests | **done** |

`metaedit reindex` derives the structured layer from the raw archive: entity rows
for artists, albums and tracks, tag edges with real popularity counts, similar
artists, autocorrect aliases and an archive-wide tag aggregate. It is reproducible
by construction — every derived primary key is assigned deterministically, never by
a sequence — and `--dry-run` reports the delta without writing.

The SPA still shows only the archive's capacity and contents plus an
API-connectivity panel; the browser and editor screens are not built yet, and
nothing yet maps Last.fm data onto Jellyfin fields (that is phase 4).

Known deviations from the original plan:

- `reindex --since` parses and validates its timestamp but performs a full rebuild
  rather than a partial derivation. Recorded in
  [`docs/design/0003`](docs/design/0003-derivation-and-reindex.md) §7 with the
  reasoning; it cannot produce wrong data, only cost time.
- `BaseItemDto` has no rating field that survives a music-library round trip, so
  `CommunityRating`/`CriticRating` are read and preserved but never proposed as
  changes.
- `lastfm_response.observation_count` counts observations *including* the first.
  `observation_count = 1` means "seen once", which is the intuitive reading.

See [`docs/adr/`](docs/adr/README.md) for the decisions behind the design, and
[`docs/design/`](docs/design/README.md) for the implementation contracts that are
fixed ahead of each phase.

## Prerequisites

- Docker with Compose (Postgres 18 runs as `docker.io/postgres:18-trixie`)
- A Jellyfin server with an **administrator** API key — metadata writes are gated
  on the `RequiresElevation` policy
- A Last.fm API key from <https://www.last.fm/api/account/create>

## Run it

```bash
cp .env.example .env
# then edit .env: JELLYFIN_URL, JELLYFIN_API_KEY, LASTFM_API_KEY, POSTGRES_PASSWORD

docker compose up -d --build
open http://127.0.0.1:8080
```

The compose stack runs Postgres, applies migrations as a one-shot `migrate`
service, and only then starts the app. The app port is bound to `127.0.0.1` by
default: it holds an administrator Jellyfin key and has no authentication of its
own. Put an authenticating proxy in front of it before exposing it anywhere.

### Endpoints available today

| Endpoint | Meaning |
|---|---|
| `GET /api/health` | Liveness. Touches nothing — safe for container health checks. |
| `GET /api/health/ready` | Readiness. Reports Postgres, Jellyfin and Last.fm independently. |
| `GET /api/info` | Versions, whether the Jellyfin key can actually write, archive config. |
| `GET /api/archive/stats` | Payload bytes against the cap, partition sizes, derived counts, archive read metrics. |
| `GET /api/archive/entities` | Search the derived layer offline: `kind`, `tag`, `search`, paging. |
| `GET /api/archive/entities/{kind}/{id}` | One derived entity with its tags, and similar artists for an artist. |
| `GET /api/archive/tags` | Every tag across the archive with how many distinct entities carry it. |
| `GET /api/archive/aliases` | Last.fm autocorrect corrections. |
| `POST /api/archive/reindex` | Rebuild the derived layer. **`dry_run` defaults to true** over HTTP. |
| `GET /api/archive/diagnose` | What is archived and what the derivation cannot use. |
| `GET /api/libraries` | Music libraries only (`CollectionType=music`). |
| `GET /api/items` | Browse artists, albums or songs; `search`, paging, `missing_metadata`. |
| `GET /api/items/{id}/state` | The full writable field set for one item, plus `etag` and lock flags. |
| `POST /api/items/states` | The same, batched — the multi-select path for bulk editing. |
| `POST /api/items/{id}/refresh` | Ask Jellyfin to re-run *its* providers. Separate from our edits by design. |
| `POST /api/items/{id}/candidates` | Archived Last.fm entities matching this item, scored. Reads only. |
| `POST /api/items/{id}/diff` | The exact changes applying a candidate would make. **Writes nothing.** |
| `POST /api/items/{id}/apply` | Apply a reviewed change set. Requires `confirm: true`. |
| `GET /api/items/{id}/snapshots` | Snapshot history, newest first. |
| `POST /api/snapshots/{id}/revert` | One-click undo. Requires `confirm=true`. |
| `POST /api/bulk/diff` | **SSE.** Diff every item in a selection; returns a `job_id`. Writes nothing. |
| `POST /api/bulk/apply` | **SSE.** Apply a reviewed `job_id`. Requires `confirm: true`. |
| `POST /api/bulk/{batch_id}/revert` | **SSE.** Undo a whole batch. Requires `confirm=true`. |
| `GET /api/bulk/jobs` | Reviewed diffs still available to apply. |

### Editing safely

The edit flow is ordered so that a write is never reachable without first seeing what
it would do, and "apply whatever Last.fm says" is not expressible:

```
candidates  →  diff  →  apply  →  snapshots  →  revert
 (reads)     (writes   (writes)    (reads)     (writes)
             nothing)
```

Four properties hold on every write, and each is tested:

1. **The payload is always the complete writable field set.** `POST /Items/{id}` is a
   full overwrite, so a field omitted from the body is nulled. Anything not selected
   is carried through at its current value instead (ADR 0003).
2. **A snapshot is written before the item is.** A failed write leaves a harmless
   orphaned snapshot; the reverse ordering would leave an unrecoverable edit.
3. **A stale `Etag` is refused with 409** and nothing is written, so a reviewed change
   set cannot silently revert an edit made in Jellyfin meanwhile (ADR 0007).
4. **Only planned fields may be selected.** Naming any other field is a 422, because
   on a full-overwrite API a field the caller can name but we did not plan is one they
   could destroy.

`confirm: true` is required on apply, and an omitted selection means the plan's
default — which is empty for any match that needs review, so an unreviewed match
writes nothing at all.

### Bulk editing

Bulk extends the same model, because a mapping mistake at this scale is a
library-wide event rather than a curiosity. `POST /api/bulk/diff` streams a per-item
diff and returns a `job_id`; `POST /api/bulk/apply` requires that job id, so an apply
is never the first request of a session.

- **Only pre-selected fields are written.** A field is pre-selected only when the
  match was confident enough to act on *unreviewed*. An item with no trustworthy
  candidate is reported as **skipped with a reason**, never guessed at — dropping it
  silently would make a batch look complete when it was not.
- **Failures are isolated per item.** A failure on item 37 does not roll back the 36
  already written, so the operator can see exactly where it stopped and the batch
  revert still covers what did happen.
- **Every write shares a `batch_id`**, persisted on the snapshots, so the whole run
  reverts as one unit. The batch identity outlives a restart even though the diff job
  does not — diff jobs are deliberately in-memory because a diff is a pure function of
  the archive and the current item state, and persisting it would create stale state.

Responses are Server-Sent Events, so a long run reports each item as it happens
rather than at the end.

## Server requirements

- **Jellyfin ≥ 10.9** for the item-update semantics this app depends on, with an
  **administrator** API key. `POST /Items/{itemId}` is gated on the
  `RequiresElevation` policy; a non-admin key is detected at startup and reported
  by `GET /api/info` as `key_is_not_elevated`.
- The Jellyfin write contract is pinned by a vendored OpenAPI spec and asserted in
  `tests/contract/`, so a breaking server change fails CI rather than corrupting a
  library.
- **A read-only key is enough through phase 4.** Metadata writes require the
  `RequiresElevation` policy, so phase 5 needs an administrator key; until then a
  non-admin key is sufficient and `GET /api/info` reports `key_is_not_elevated`
  rather than failing.
- **Postgres 18** is required: the archive uses declarative monthly partitioning
  on an append-only table.

## Local development

Python dependencies are managed with [`uv`](https://docs.astral.sh/uv/); the SPA
with `pnpm`.

```bash
# One-time: install uv locally and create the virtualenv
curl -LsSf https://astral.sh/uv/install.sh | UV_INSTALL_DIR="$PWD/.tools" UV_UNMANAGED_INSTALL=1 sh
export PATH="$PWD/.tools:$PATH"

# Some environments keep $HOME read-only; keep every cache inside the workspace.
export UV_CACHE_DIR="$PWD/.cache/uv"

uv venv --python 3.13
uv sync --extra dev

# Postgres only, then migrate
docker compose up -d postgres
uv run alembic upgrade head
uv run metaedit partitions          # pre-create monthly archive partitions

uv run uvicorn metaedit.main:app --reload --port 8080
```

For SPA hot reload, run Vite separately — it proxies `/api` to the FastAPI
process:

```bash
cd apps/web
pnpm install
pnpm dev          # http://127.0.0.1:5173
```

In a sandboxed environment pnpm may be unable to write its operation lock outside
the workspace. Either build the SPA through Docker (`docker compose build app`),
or redirect the store and XDG directories into the workspace:

```bash
cd apps/web
export XDG_CACHE_HOME="$PWD/.pnpm-cache" XDG_DATA_HOME="$PWD/.pnpm-data" \
       XDG_STATE_HOME="$PWD/.pnpm-state" npm_config_cache="$PWD/.npm-cache"
pnpm install
```

## Checks

```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run pytest
```

[`.github/workflows/ci.yml`](.github/workflows/ci.yml) runs the same four gates, plus
the SPA typecheck/build and a deployment check that builds the image, brings the
compose stack up with `--wait`, and probes the API — so a green run means the
container actually starts, not merely that it compiles. `uv` and the Postgres
image are pinned to the versions used locally, and the workflow asserts the
service really is Postgres 18, because a silent fallback to another major version
would invalidate the partition tests.

Integration tests need a reachable Postgres 18; they create and drop a disposable
database per test, so they never touch the data in `metaedit`. Point them
elsewhere with `METAEDIT_TEST_DATABASE_URL` (default
`postgresql+psycopg://metaedit:metaedit@localhost:5432/metaedit`).

### Live tests

The Jellyfin and Last.fm adapters are verified against recorded fixtures and a
stubbed transport. That is not the same as verified against a real server, so there
are opt-in live tests that settle the assumptions fixtures cannot answer. Both are
**read-only**: no Last.fm write method is ever called (`artist.addTags` and friends
are not implemented), and no Jellyfin write endpoint is touched until phase 5.

```bash
# Both read .env, so no exporting is needed.
uv run pytest -m live_lastfm -v -s     # one real call per method, then reindex on real data
uv run pytest -m live_jellyfin -v -s   # auth, libraries, field round-tripping, Etag
```

They are **deselected by default** (`addopts` in `pyproject.toml`): credentials in
`.env` would otherwise make an ordinary `pytest` reach real services, which is slow,
rate-limited and non-deterministic.

Both take `-s` deliberately: the tests print what they found, which is the point.

### Live-verified behaviour

Both suites have now been run against real services (a self-hosted Jellyfin 12.2.0, and
the live Last.fm API). Findings that contradicted the code:

**Jellyfin**

- **Authentication is the `MediaBrowser` scheme**, not a bare key:
  `Authorization: MediaBrowser Client="…", Device="…", DeviceId="…", Version="…", Token="…"`.
  Jellyfin 12 **disabled the legacy channels**, so `Authorization: <key>`,
  `X-Emby-Token` and `?api_key=` all return 401. A bare token without the scheme is
  parsed as a malformed scheme and returns 400.
- **An API key is userless**, so `GET /Users/Me` answers **400** — by design. The
  body is a generic RFC9110 ProblemDetails that does *not* name the reason, so
  elevation is confirmed by a follow-up authenticated read instead. API keys carry
  administrator privileges, which is what an item update needs.
- **`/Artists` must not be given `includeItemTypes`.** Passing
  `includeItemTypes=MusicArtist` returned **0** results while a bare call returned
  **594**. A filter that looks harmless silently emptied the browse.
- **`/Library/MediaFolders` omits `ItemId`** for every library, so there was no id to
  scope a browse by. `/Library/VirtualFolders` supplies it.
- **`Etag` exists but only when requested via `fields=`.** Concluding otherwise would
  have sent phase 5 down an unnecessary fallback. `DateLastSaved` is *not* returned
  for music items even when requested, so `Etag` is the version token for ADR 0007.
- **`GET /Items/{itemId}` requires a user context.** With a userless API key it returns
  **400** ("Error processing request.") for *every* id — including one the server itself
  just returned from `/Artists` — unless `userId` is supplied. The list endpoints
  (`/Items?ids=…`) tolerate its absence, which is what made this easy to miss: every
  list-based test passed while the entire single-item path was broken. The contract
  lists `userId` as *optional*, so a schema check agrees with the broken assumption.

  This mattered a great deal, because apply **reads the item first**: the failure
  surfaced as `Jellyfin rejected the request to /Items/… with 400` and no write was ever
  attempted. The client now discovers a user (an explicit `JELLYFIN_USER_ID` wins,
  otherwise the first administrator in sorted-id order is cached per process).
- Item type names (`MusicArtist`, `MusicAlbum`, `Audio`) are correct — 639 artists,
  502 albums, 5442 songs on that server.
- Every requested `fields=` value is honoured **except `ParentId`**, which is
  returned anyway as part of the base DTO on some endpoints. No whitelist field is
  omitted, so a read can round-trip the write payload.

**Last.fm**

- `artist.getTopTags` **does** carry `count` (10/10 tags). It is also **envelope-free**
  (`{"toptags": ...}`, no `{"artist": ...}` wrapper), so the entity derivation skipped
  it entirely and every `lastfm_tag_edge.count` was null — which silently disabled
  `TagPolicy.min_count` and reduced genre ranking to list order everywhere. The counts
  are now folded into the artist entity *and* its tag edges; applying them only to the
  edges would have looked correct while still filtering nothing, because the mapping
  layer reads the entity. Album and track tag lists carry no counts anywhere in the API,
  so their ranking stays list order by design rather than by accident.
- `artist.getsimilar` puts the **peer list** under `artist` and loses the owning
  artist's name, confirming the owner must come from the request params.
- `album.getInfo` returns tags under **`tags`** (no counts) and leaves `toptags`
  absent, so album tag ranking falls back to list order.
- `album.getInfo` **does** return a usable `wiki.summary`. The phase 3 code asserted
  the opposite and discarded it, so **album overviews were silently unavailable** —
  fixed by migration `0002_album_overview`.
- `album.getInfo` returns **no `releasedate`**, by name or by MBID, so album years
  cannot be sourced from it. The column is retained for tolerance and stays null.

Still needs a write, so it is added in phase 5: whether `Genres` writes create genre
entities immediately.

## The archive

Every Last.fm request is stored, in three layers:

1. **Raw** — `lastfm_request` logs one row per HTTP attempt (success *or*
   failure) in a monthly partition; `lastfm_response` stores response bodies
   content-addressed by body hash, so an unchanged body is stored once while
   every observation of it is still recorded.
2. **Derived current state** — `lastfm_artist` / `lastfm_album` / `lastfm_track`,
   one row per entity, each pointing at the `latest_response_id` it came from.
3. **Derived graph** — `lastfm_tag_edge`, `lastfm_similarity`,
   `lastfm_artist_alias`, `lastfm_entity_tag`.

Layers 2 and 3 are the contract for phase 3: they must be reproducible from
layer 1 alone, with no network access. Their tables exist but nothing populates
them yet.

`lastfm_request` logs **HTTP attempts to Last.fm only**. A lookup answered from the
archive is the case where no attempt was made, so it is not written there; those
reads are counted in process memory and reported by `GET /api/archive/stats` under
`reads`, alongside `stray_archive_reads`, which counts any historical rows written
before that distinction was enforced.

Operational commands:

```bash
uv run metaedit archive-stats                    # bytes stored vs. the cap
uv run metaedit partitions --months 6            # extend the partition window
uv run metaedit prune-raw --keep-days 365        # report; add --yes to actually drop
uv run metaedit reindex                          # rebuild the derived layer
uv run metaedit reindex --dry-run                # what a rebuild would change
uv run metaedit reindex --only artist            # one table family
uv run metaedit archive-entities                 # what is archived, and what is not understood
```

`reindex` needs no network access and never writes to the raw layer, so it is safe
to run at any time, and safe to interrupt: the swap is one transaction, so a failure
leaves the previous derived tables untouched. Its `--dry-run` is the reviewer's tool
for a parsing or policy change.

Because the failure mode of a parsing mismatch is *absence* — fewer entities, with
every count downstream still looking plausible — the report carries a
`unexpected_shapes` count. **Zero is healthy.** A rise means archived bodies are no
longer understood, and `metaedit archive-entities` names the offending method and
payload keys.

The contract it implements is fixed in
[`docs/design/0003-derivation-and-reindex.md`](docs/design/0003-derivation-and-reindex.md).

**Nothing is deleted automatically.** The Last.fm API Terms of Service cap stored
Last.fm Data at 100 MB; that number is measured and displayed, and pruning is
always an explicit operator decision (`prune-raw` reports what it would drop and
refuses to act without `--yes`).

Retention is monthly, because partitions are: `prune-raw --keep-days N` lists
only whole months that are entirely older than the window, and deliberately keeps
the cutoff month and the month before it too. A shallow window therefore reports
nothing rather than risking the loss of recent observations.

## Attribution

Metadata originates from Last.fm. The UI links every Last.fm-derived value back to
the corresponding artist, album or track page on <https://www.last.fm>, as the
[API Terms of Service](https://www.last.fm/api/tos) require. This tool is for
personal, non-commercial use.

## Layout

```
src/metaedit/
  domain/         pure logic: writable fields, snapshots, tag policy, mapping, diff
  adapters/       jellyfin/ and lastfm/ HTTP clients
  archive/        archive writes, derivation, reindex, byte accounting
  db/             SQLAlchemy models, sessions, partition management
  api/            FastAPI routers
apps/web/         React + TypeScript SPA
contracts/        vendored Jellyfin OpenAPI spec (pinned, asserted in tests)
docs/adr/         architecture decision records
migrations/       Alembic, forward-only
```
