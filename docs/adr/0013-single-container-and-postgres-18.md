# 0013. Single container serving the SPA; Postgres 18 pinned as `18-trixie`

- **Status:** Accepted
- **Date:** 2026-10-06

## Context

The app is a small API plus a small single-page UI for one operator. It needs
relational storage with JSONB and real partitioning for the archive, and the
operator explicitly asked for Postgres 18 on a Debian trixie base.

## Decision

- One application container. FastAPI serves both `/api/*` and the built SPA,
  with a catch-all route that falls back to `index.html` for client-side routes.
- Postgres 18 is pinned as `docker.io/postgres:18-trixie` (currently 18.6) and
  given a named volume. Migrations run as a separate, idempotent compose service
  gated on `service_completed_successfully`, never implicitly at app startup.
- The app binds to `127.0.0.1` by default in compose.

## Consequences

- One deployable, one dependency, no reverse proxy to configure, no CORS.
- Serving the SPA from the API process means the SPA is only as available as the
  API, which is correct: without the API the UI can do nothing.
- Postgres 18 specifics are relied upon deliberately: `uuidv7()` is available and
  declarative `PARTITION BY RANGE` on the monthly request log needs no extension.
- The 18+ images mount data at `/var/lib/postgresql` (a version-specific
  subdirectory), not `/var/lib/postgresql/data`; the compose mount point reflects
  that, and note that changing it later requires care.
- A static file server in front could be added later without changing the API.

## Alternatives rejected

- **Two containers (nginx + API).** Rejected: real extra surface (proxy config,
  two health checks, cache headers) for no benefit at this size.
- **`postgres:18-alpine`.** Rejected: the operator asked for trixie, and glibc
  collation behaviour is more predictable for a long-lived archive.
- **SQLite instead of Postgres.** Rejected: the archive wants concurrent readers
  during long reindexes, real JSONB indexing, and partitioning of an
  append-only log.
