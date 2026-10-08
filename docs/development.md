# Development

Everything needed to run `metaedit` from a source checkout, run its checks, and
work on the SPA. For running the released container, the [README](../README.md)
quickstart is shorter.

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

## Local development

Python dependencies are managed with [`uv`](https://docs.astral.sh/uv/); the SPA
with `pnpm`.

Start from a configured `.env` — see the [README quickstart](../README.md#quickstart)
for the two API keys it needs.

```bash
# One-time: install uv locally and create the virtualenv
curl -LsS https://astral.sh/uv/install.sh | UV_INSTALL_DIR="$PWD/.tools" UV_UNMANAGED_INSTALL=1 sh
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

[`.github/workflows/ci.yml`](../.github/workflows/ci.yml) runs the same four gates, plus
the SPA typecheck/build and a deployment check that builds the image, brings the
compose stack up with `--wait`, and probes the API — so a green run means the
container actually starts, not merely that it compiles. `uv` and the Postgres
image are pinned to the versions used locally, and the workflow asserts the
service really is Postgres 18, because a silent fallback to another major version
would invalidate the partition tests.

Integration tests need a reachable Postgres 18. Each test gets a disposable
database of its own, cloned from a template that is migrated once per session, so
the migration chain runs once instead of once per test. They never touch the data
in `metaedit` itself: the template and the per-test copies are created and
dropped by the suite. Point it at another server with
`METAEDIT_TEST_DATABASE_URL` (default
`postgresql+psycopg://metaedit:metaedit@localhost:5432/metaedit`).

### Live tests

The Jellyfin and Last.fm adapters are verified against recorded fixtures and a
stubbed transport. That is not the same as verified against a real server, so there
are opt-in live tests that settle the assumptions fixtures cannot answer. Both are
**read-only**: no Last.fm write method is ever called (`artist.addTags` and friends
are not implemented), and no Jellyfin write endpoint is touched by them.

```bash
# Both read .env, so no exporting is needed.
uv run pytest -m live_lastfm -v -s     # one real call per method, then reindex on real data
uv run pytest -m live_jellyfin -v -s   # auth, libraries, field round-tripping, Etag
```

They are **deselected by default** (`addopts` in `pyproject.toml`): credentials in
`.env` would otherwise make an ordinary `pytest` reach real services, which is slow,
rate-limited and non-deterministic.

Both take `-s` deliberately: the tests print what they found, which is the point.

Note that `addopts` already sets `-q`, and pytest reads a second `-q` as a further
drop in verbosity — which suppresses the summary line entirely. CI does not pass
one, so a green run reports how many tests ran.

### Upstream fidelity tests

`tests/support/` holds in-process fakes of Jellyfin and Last.fm that reproduce what the
real servers do — including the parts that cost real debugging time — and **reject what
they reject**. `tests/integration/test_upstream_fidelity.py` runs the app against them.

This exists because of a pattern worth naming: every bug that reached a user in this
project was found by talking to the real servers, and **none** by the test suite. Each
integration test carried its own handful of permissive route branches, so the mocks agreed
with the client's assumptions rather than the servers' behaviour. A mock that accepts
everything cannot fail, and a test that cannot fail is not verification.

The fakes encode, each with a comment naming the live observation behind it:

| Behaviour | Why it matters |
| --- | --- |
| `GET /Items/{id}` is **400** without `userId` | Broke the whole edit path: apply reads before writing, so the 400 looked like a write failure and no write was attempted |
| `/Users/Me` is **400** for an API key | A *success* signal for a userless credential; treating it as failure hides the entire editor |
| `GET /Artists` empties with `includeItemTypes` | A harmless-looking filter that blanks the browse |
| `/Library/MediaFolders` omits `ItemId` | Leaves a browse with no id to scope by |
| `Etag` only when requested | Without it, optimistic concurrency looks unimplementable |
| `getTopTags` is envelope-free, `getInfo` tags have no counts | Popularity can only come from the top-tags call |
| Last.fm spells one album with a U+2026 ellipsis and the same album with three dots | Counts silently dropped until the join tolerated it |
| `POST /Items/{id}` nulls every field absent from the body | The hazard the whole tool is shaped around — the fake really performs the overwrite, so a dropped field visibly destroys data |

Verified by reverting the corresponding fixes: the suite fails, and passes when they are
restored.

## End-to-end tests

Fifteen Playwright specs cover the critical journey through a real browser: browse,
search, review a diff, apply one field, undo it, revert a batch, and inspect the archive.

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

These specs are **not part of CI**. They need a browser and a compose stack, and they
were last run by hand against the stubs. Treat a green CI run as saying nothing about
them.

## The API contract

The SPA's types are **generated** from the backend's OpenAPI document rather than
hand-written, and a CI step fails if a schema change was not regenerated:

```bash
uv run python scripts/export_contract.py           # regenerate
uv run python scripts/export_contract.py --check   # what CI runs
```

Query parameters are typed too, which is not decoration: the library page once sent
`limit` to an endpoint that takes `page_size`, so every page silently came back at the
default size. With the parameter types derived from the same document, that is now a
compile error with an actionable message.

Two consequences worth knowing before editing:

- `info.version` lives *in* the document, so a version bump requires regenerating the
  contract or `--check` fails.
- The script has no argument parsing to speak of. `--help` regenerates rather than
  printing help.

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
