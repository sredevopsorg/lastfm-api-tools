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

Under construction, phase by phase. **Stopped deliberately after phase 2**, at the
owner's request, before derivation (`reindex`) is implemented.

| Area | State |
|---|---|
| Container, Postgres 18, migrations, health endpoints | **done** |
| Archive schema (raw + derived), byte accounting vs. the ToS cap | **done** |
| SPA shell served by the API | **done** |
| Jellyfin read: libraries, browse, batch item state, full write whitelist | **done** |
| Last.fm client: typed models, shared rate limiter, backoff, archive-first reads | **done** |
| Archive writes: append-only request log, content-addressed bodies, cap policy | **done** |
| Derivation into entity/tag/similarity tables, `reindex` | **next** |
| Tag policy, mapping, confidence, diff | planned (phase 4) |
| Apply, snapshot, undo, bulk editing | planned (phases 5–6) |
| Library browser, editor, archive explorer UI | planned (phase 7) |

The archive tables therefore exist and are populated by every Last.fm call, but
nothing derives from them yet: `metaedit reindex` raises `NotImplementedError`
by design. The SPA shows the archive's capacity and contents and an
API-connectivity panel; the browser and editor screens are not built yet.

Known deviations from the original plan, to settle before phase 4:

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
| `GET /api/archive/stats` | Stored payload bytes against the Last.fm storage cap, plus partition sizes. |
| `GET /api/libraries` | Music libraries only (`CollectionType=music`). |
| `GET /api/items` | Browse artists, albums or songs; `search`, paging, `missing_metadata`. |
| `GET /api/items/{id}/state` | The full writable field set for one item, plus `etag` and lock flags. |
| `POST /api/items/states` | The same, batched — the multi-select path for bulk editing. |
| `POST /api/items/{id}/refresh` | Ask Jellyfin to re-run *its* providers. Separate from our edits by design. |

## Server requirements

- **Jellyfin ≥ 10.9** for the item-update semantics this app depends on, with an
  **administrator** API key. `POST /Items/{itemId}` is gated on the
  `RequiresElevation` policy; a non-admin key is detected at startup and reported
  by `GET /api/info` as `key_is_not_elevated`.
- The Jellyfin write contract is pinned by a vendored OpenAPI spec and asserted in
  `tests/contract/`, so a breaking server change fails CI rather than corrupting a
  library.
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

Integration tests need a reachable Postgres 18; they create and drop a disposable
database per test, so they never touch the data in `metaedit`. Point them
elsewhere with `METAEDIT_TEST_DATABASE_URL` (default
`postgresql+psycopg://metaedit:metaedit@localhost:5432/metaedit`).

Tests marked `live_lastfm` and `live_jellyfin` are skipped unless the matching
credentials are configured. **No live test has been run yet**: the Jellyfin and
Last.fm adapters are verified against recorded fixtures and a stubbed transport,
not against a real server. Four behaviours are therefore still assumptions and
must be confirmed against a live instance before phase 5 (see the open items in
the project plan):

1. the exact `Authorization` header encoding this Jellyfin version accepts;
2. whether `Etag` actually changes after an `UpdateItem`;
3. whether `Genres` writes create genre entities immediately or only on the next
   library scan;
4. whether the fields requested via `fields=` are all returned (the snapshot
   fills any that are not, and logs `jellyfin_field_missing` when that happens —
   watch for that warning on a real server).

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

Operational commands that work today:

```bash
uv run metaedit archive-stats                    # bytes stored vs. the cap
uv run metaedit partitions --months 6            # extend the partition window
uv run metaedit prune-raw --keep-days 365        # report; add --yes to actually drop
```

Reserved for phase 3 (currently raises `NotImplementedError` so it cannot be
mistaken for working). The contract it must satisfy is fixed in
[`docs/design/0003-derivation-and-reindex.md`](docs/design/0003-derivation-and-reindex.md):

```bash
uv run metaedit reindex --dry-run                # what a model change would alter
```

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
