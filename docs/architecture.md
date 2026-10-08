# Architecture and status

What is implemented, what is deliberately not, and where the design decisions live.
For the reasoning behind a decision, read the ADR it names rather than this page.

## Status

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

## The UI

React + Vite single-page app, served by the same container as the API. Five routes:

| Route | What it does |
| --- | --- |
| `/` **Library** | Browse artists, albums or songs; filter to items missing metadata; open the editor. |
| `/edit/:itemId` **Editor** | Pick an archived candidate, review the diff field by field, write only what you tick, undo from the snapshot history. |
| `/review` **Review** | The pre-write review of what a candidate would change. |
| `/bulk` **Bulk** | Review a selection, apply the reviewed job, watch per-item progress, revert the batch as a unit. |
| `/archive` **Archive** | Storage-cap headroom, the derived layer's counts, stored entities with tags by popularity and similar artists. |

Reaching these routes is the *only* thing the SPA fallback in `main.py` does
besides serving built assets, which is why the containment check on it matters.

## Layering

```
api  →  service  →  domain  ←  adapters
```

Dependency direction is enforced: adapters depend on the domain, never the reverse,
and `domain/` is pure logic with no I/O — writable fields, snapshots, tag policy,
mapping, diff. See ADRs [0001](adr/0001-custom-app-over-jellyfin-plugin.md) (a custom
app calling the Jellyfin REST API, not a plugin) and
[0013](adr/0013-single-container-and-postgres-18.md) (one container plus Postgres 18).

## Known deviations from the original plan

- `reindex --since` parses and validates its timestamp but performs a full rebuild
  rather than a partial derivation. Recorded in
  [`docs/design/0003`](design/0003-derivation-and-reindex.md) §7 with the
  reasoning; it cannot produce wrong data, only cost time.
- `BaseItemDto` has no rating field that survives a music-library round trip, so
  `CommunityRating`/`CriticRating` are read and preserved but never proposed as
  changes.
- `lastfm_response.observation_count` counts observations *including* the first.
  `observation_count = 1` means "seen once", which is the intuitive reading.
- Related artists (`lastfm_similarity`) are stored and displayed but are **never**
  written to Jellyfin — there is no field for them, and inventing one would put data
  in a place the server does not model (ADR
  [0005](adr/0005-related-artists-in-our-db-only.md)).

## Where the design lives

- [`docs/adr/`](adr/README.md) — the decisions, with context, consequences and the
  alternatives rejected. Each is immutable; a change is a new record, not an edit.
- [`docs/design/`](design/README.md) — implementation contracts that are fixed ahead
  of a phase: exactly what the code must do, where an ADR records only why.
- [`docs/api.md`](api.md) — endpoint reference and the write-safety rules.
- [`docs/operations.md`](operations.md) — the archive, the CLI, retention.
- [`docs/development.md`](development.md) — checkout setup, checks, tests.

## Attribution

Metadata originates from Last.fm. The UI links every Last.fm-derived value back to
the corresponding artist, album or track page on <https://www.last.fm>, as the
[API Terms of Service](https://www.last.fm/api/tos) require. This tool is for
personal, non-commercial use.
