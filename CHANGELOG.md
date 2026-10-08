# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The version is `0.x`, which by SemVer means the interface may still change between minor
releases. The metadata *contract* is the exception: what is written to Jellyfin is
specified by ADRs 0003, 0004 and 0007 and those guarantees are treated as stable.

## [Unreleased]

### Changed

**Postgres-backed tests clone a migrated template instead of migrating per test.** Every
one of them ran `alembic upgrade head` in a subprocess: most of a second spent importing
Python and replaying a migration chain whose result is identical every single time. It was
not a hidden cost either — 132 tests had a measurable setup, adding up to 119 s of the
139 s the integration suite took, which is to say the suite was mostly replaying
migrations.

The chain now runs once per session, into a template database, and each test's database is
copied from it with `CREATE DATABASE ... TEMPLATE`. Every test still gets a private, empty,
fully migrated database; only the way it is populated changed.

- The integration suite went from **139 s to 36 s**, and the whole suite from **149 s to
  40 s**, on the machine that measured it.
- A broken migration now fails the session before any test reports a result, instead of
  being attributed to whichever test happened to run first.
- The isolation guarantee is what made the suite trustworthy in the first place, so it is
  now asserted rather than assumed: `test_test_database_isolation.py` writes a row into one
  clone and checks the other cannot see it, and checks that a clone really is at the
  migration head — `alembic_version` is copied rather than applied, and a template built
  with `create_all` would leave the table empty while every schema test still passed.

### Added

`test_spa_static.py` covers the containment check in the SPA fallback, which had no test.
It was not decorative: deleting `candidate.is_relative_to(dist)` makes
`GET /..%2Fsecret.txt` return a file from outside the bundle, which is verified in the test
itself by the fact that the check is the only thing standing in the way. The request is
percent-encoded deliberately — a literal `../` is collapsed by the client and the server
before the route sees it, so only an encoded separator actually delivers `../` as the path
parameter. Without that detail the test would have passed for the wrong reason.

### Notes

- **The four remaining CodeQL alerts are dismissed as false positives**, with the reasoning
  recorded on each alert so the next reader does not have to redo the analysis. Both rules
  are tripped by code that is correct:
  - `py/path-injection` (three alerts, `main.py`) — the alert is on the file read, and the
    query does not model `pathlib.Path.is_relative_to` as a guard. The guard is there, and
    now tested.
  - `py/weak-sensitive-data-hashing` (`canonical.py`) — the hash is a 16-character
    *correlation* fingerprint so the archive can say which key fetched a payload without
    storing the key. It is not password storage and is never used to verify anything.
- Every CI job now runs on `ubuntu-latest`. The previous state was mixed: three jobs on
  `ubuntu-latest` and the Python job still on `ubuntu-24.04`, which is an inconsistency
  dressed up as a pin. The deliberate trade is recorded in the workflow: the runner moves
  on its own, and a job that breaks on a new runner is more useful than one that quietly
  keeps testing an old one. The versions the tests are actually sensitive to — Python, uv,
  Node and the Postgres image — are pinned explicitly instead.
- The repository was renamed to `metaedit`, so the in-repo links and the Last.fm
  `User-Agent` URL now name it. They had been relying on GitHub's redirect for renamed
  repositories, which works right up until someone creates a repository under the old
  name — at which point the links quietly point somewhere else. `CHANGELOG.md` and
  `src/metaedit/config.py` are the only two files affected.

## [0.0.2] - 2026-10-08

### Fixed

**Internal error text no longer reaches the browser.** CodeQL found this
(`py/stack-trace-exposure`), and it found it *twice* — which is the interesting part,
because the second instance was still there after the first fix. Each place that reported
a failure was answering "is this exception safe to quote?" for itself, and one of them
answered wrong.

- The SSE catch-all put `str(exc)` straight into the `error` frame. An unexpected failure
  now reports a `code` and a short **reference id**; the traceback goes to the server log
  under that same reference, so a report from a user still leads to the cause.
- The same expression appeared a second time in the per-item failure payloads for bulk
  apply and revert. Both now go through one function.
- `domain.errors.public_error_text` is that function, and the single definition of what a
  client may be told. The distinction it draws is **provenance, not severity**: a
  `MetaeditError` message was written by us for a reader and passes through; anything
  else gets a reference.
- **No credential was ever reachable**, which was checked rather than assumed: Last.fm's
  `httpx` failures collapse to a type name before escaping, so the key in the query string
  never reached a message; the Jellyfin key travels in a header and its bodies already
  pass through `_sanitise_body`; and a failing Postgres connection renders with the
  password masked. What leaked was internal topology — driver names, host and port.
- `SelectionError` now subclasses `MetaeditError` instead of `ValueError`. It was an
  expected failure with a person-readable message all along, and being a bare `ValueError`
  meant the write path had two hierarchies to catch and its HTTP status lived in a handler
  in another layer. The status (`422`) and code (`invalid_request`) are unchanged, but they
  now sit next to the failure that causes them, and the per-item failure payload no longer
  needs to special-case it.

`domain/errors.py` had promised this from the start — "no upstream payload ever reaches a
client verbatim" — so both leaks were omissions, not decisions.

Found by [CodeQL code scanning](https://github.com/sredevopsorg/metaedit/security/code-scanning),
which was enabled on this repository between the two releases.

### Notes

- GitHub Actions were brought to current majors (`actions/checkout` v7,
  `actions/upload-artifact` v7, `actions/setup-node` v7) and `astral-sh/setup-uv` to
  v10.1.0, pinned to a commit SHA so a moved tag cannot change what CI runs.
- This is a fix release, so nothing in the metadata contract changed. The API version
  reported by `/openapi.json` is the only interface difference.

## [0.0.1] - 2026-10-08

The first release: a working Last.fm → Jellyfin metadata editor with a persistent local
archive, a review step before every write, and one-click undo.

### Added

**Editing flow.** Search the Jellyfin library, select one or many items, fetch their
Last.fm data, review the proposed changes field by field, confirm, and write. Every write
is snapshotted first and can be reverted, individually or as a batch.

- **Library browser** over artists, albums and songs, with search, a "missing metadata"
  filter, and multi-select.
- **Fetch from Last.fm** — derives a query per item (preferring a MusicBrainz id over a
  name), fetches `getInfo`, `getTopTags` and `getSimilar`, archives every response, and
  rebuilds the derived layer. A miss is a reported outcome with close matches, not an
  error, so a library-wide fetch does not abort on the first obscure item.
- **Review screen** showing each item's proposed changes with an independent tick per
  field, starting from the policy's safe default. An item nobody reviewed writes nothing.
- **Apply and revert** with a snapshot taken before every write, per-item audit entries
  carrying Last.fm provenance, and optimistic concurrency on the item's `Etag`.
- **Bulk editing over Server-Sent Events** — review a selection, apply it, watch per-item
  progress, and revert the whole batch. A failure on one item does not roll back or
  abandon the rest.
- **Archive explorer** — storage-cap headroom, the derived layer's counts, and stored
  entities with tags by real popularity and their similar artists.

**Last.fm archive.** A persistent, incremental, structured store of everything fetched,
so the same request is never paid for twice.

- Content-addressed responses: identical bodies are stored once while every request
  against them is logged, so observations normally exceed distinct bodies.
- Append-only request log in monthly partitions, with the storage cap measured and
  surfaced rather than silently pruned (ADR 0011).
- Three layers — raw, derived current state, derived graph — where the derived layer is a
  pure function of the raw one. A rebuild is reproducible: the md5 over all seven derived
  tables is identical across a wipe and rebuild, and the raw layer is untouched.

**Domain layer.** Tag policy (genre/style limits, minimum counts, blacklist, and
merge/replace/fill-if-empty modes per field), HTML-to-text sanitisation of biographies,
weighted confidence scoring with auto/review/reject verdicts, and a diff that only ever
proposes what policy permits.

**API and contract.** Typed response models for every endpoint, so the OpenAPI document
describes what the server actually returns. The SPA's types — including query parameters —
are generated from that document rather than hand-written, and CI fails if a schema change
was not regenerated.

**Operations.** Multi-stage Dockerfile running as a non-root user, `docker-compose` with
`docker.io/postgres:18-trixie`, migrations as a separate idempotent step, health and
readiness endpoints, and structured logging.

### Fixed

Bugs found only by running against real Jellyfin 12.2.0 and the live Last.fm API. Each is
recorded because the *class* of mistake is more instructive than the instance.

- **Every single-item read returned 400.** `GET /Items/{itemId}` needs a user context, and
  a userless API key must pass `userId` explicitly — for every id, including ones the
  server itself had just returned from `/Artists`. Because apply reads an item before
  writing, the whole edit path was dead and surfaced as a write failure that never
  attempted a write.
- **`/api/info` reported a working API key as unusable**, so the UI hid the entire editor
  on a correctly configured deployment.
- **Tag popularity never reached the tag policy.** `artist.getTopTags` is envelope-free, so
  the entity derivation skipped it and every tag count was null — silently disabling
  `min_count` and reducing ranking to list order everywhere.
- **Authentication did not work on Jellyfin 12**, which removed the legacy auth channels.
  A bare `Authorization: <key>` returns 401; the `MediaBrowser` scheme is required.
- **`/Artists` returned nothing when given `includeItemTypes`** — a filter that looks
  harmless and emptied the browse.
- **Album overviews were discarded.** The code asserted album `getInfo` carries no wiki;
  it does, and the text was being thrown away.
- **`AlbumArtist`, `Artists` and `AlbumArtists` are three spellings** and only the array
  was read, leaving some albums with no artist and therefore no possible Last.fm lookup.
- **Last.fm spells one album with a U+2026 ellipsis and the same album with three dots**
  across its own endpoints, which silently dropped that album's popularity.
- **A miss was silent when the fallback search was disabled**, and a rejected API key was
  converted into a per-item miss instead of a fatal error.
- Archive reads were logged as Last.fm requests; a field-level mode override had no
  effect; `warn` could be configured past `refuse`; `prune-raw` exited non-zero on a
  successful dry run; and an album-only `AttributeError` reached the API as a 500.

### Security

- Security headers applied in the app: a restrictive CSP (the SPA shares an origin with the
  write endpoints, so injected script could edit metadata), `nosniff`,
  `frame-ancestors 'none'`, `Referrer-Policy: no-referrer`, and a `Permissions-Policy`
  denying camera, microphone and geolocation.
- A 2 MiB request-body cap, checked against the declared `Content-Length`.
- Both credentials are `SecretStr`, so `model_dump()` and `repr()` cannot leak them, and
  upstream error bodies are redacted before being surfaced.

### Documentation

- **13 ADRs** covering the decisions that constrain the design: writing fields directly
  rather than through Jellyfin's provider pipeline, the full-overwrite hazard, snapshotting
  before every write, related artists staying in our database, optimistic concurrency,
  the persistent archive, content addressing, measuring rather than pruning, read-only
  Last.fm, and the single-container deployment.
- **`docs/design/0003`** specifying the derivation contract, including the deliberate
  deviations from the original plan.
- A README recording every behaviour verified against live servers, so the quirks are
  recoverable rather than folklore.

### Testing

- Unit, integration (against a real Postgres, freshly created and migrated per test),
  contract, live and end-to-end suites.
- **Faithful upstream mocks**: every bug above was found by talking to the real servers and
  none by the test suite, because the mocks agreed with the client's assumptions rather
  than the servers' behaviour. The mocks in `tests/support/` now reproduce what the servers
  do — including a Jellyfin that really *performs* the full overwrite, so a payload that
  drops a field visibly destroys data.
- The end-to-end suite runs the whole journey against a stub Jellyfin and a stub Last.fm in
  a separate compose file, because the journey writes and a flag that could point it at a
  real library is a flag somebody will eventually set.

### Notes

- Last.fm is used **read-only**; only an API key is needed and no user authentication is
  performed (ADR 0012).
- Related-artist data is kept in this application's database and is never written to
  Jellyfin (ADR 0005).
- The Last.fm Terms of Service cap stored Last.fm Data at 100 MB. Usage is measured and
  shown; nothing is deleted automatically, and reaching the cap refuses new writes instead.

[Unreleased]: https://github.com/sredevopsorg/metaedit/compare/v0.0.2...HEAD
[0.0.2]: https://github.com/sredevopsorg/metaedit/releases/tag/v0.0.2
[0.0.1]: https://github.com/sredevopsorg/metaedit/releases/tag/v0.0.1
