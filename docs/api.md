# API

Every endpoint `metaedit` serves. The OpenAPI document at `/openapi.json` is the
authoritative one — the SPA's types are generated from it and CI fails if they are
stale — so this page is for reading, not for code generation.

Most of these are the UI's own backend rather than an integration surface, but
they are documented because a script is often the right way to ask a question of
the archive.

## Health and metadata

| Endpoint | Meaning |
|---|---|
| `GET /api/health` | Liveness. Touches nothing — safe for container health checks. |
| `GET /api/health/ready` | Readiness. Reports Postgres, Jellyfin and Last.fm independently. |
| `GET /api/info` | Versions, whether the Jellyfin key can actually write, archive config. |

## Jellyfin library

| Endpoint | Meaning |
|---|---|
| `GET /api/libraries` | Music libraries only (`CollectionType=music`). |
| `GET /api/items` | Browse artists, albums or songs; `search`, paging, `missing`, `artist_ids`, `album_ids`, `exclude`. |
| `GET /api/items/{id}/state` | The full writable field set for one item, plus `etag` and lock flags. |
| `POST /api/items/states` | The same, batched — the multi-select path for bulk editing. |
| `POST /api/items/{id}/refresh` | Ask Jellyfin to re-run *its* providers. Separate from our edits by design. |

### Narrowing a selection

`GET /api/items`, `POST /api/bulk/diff`, `POST /api/bulk/remove-genre/diff` and
`POST /api/harvest` accept the same three narrowings and apply the same rules — the id
shape check, the pattern semantics and the label set are one implementation
(`domain.identifiers`, `domain.exclusion`, `service.labels`). The three write paths
additionally share `service.selection`, which is where the over-fetch and the counters
live; `GET /api/items` composes the same primitives but *windows* the scan, so it reports
what it read rather than collecting a signature.

| Parameter | What it does | Cost |
|---|---|---|
| `artist_ids` | Items credited to any of these artists. `album` and `song` only. | Jellyfin's own filter; free. |
| `album_ids` | Songs on any of these albums. `song` only. | Jellyfin's own filter; free. |
| `exclude` | Case-insensitive glob patterns; an item whose **name**, **album** or **album artist** matches any of them is dropped. | A scan over the items. |
| `missing` | Items lacking any of the named aspects. | A scan over the items. |

Semantics worth stating, all verified against Jellyfin 12.2.0:

- Ids **union within a parameter** (`ArtistIds=ABBA,a-ha` is the sum of the two) and
  **intersect across** them (`ArtistIds` + `AlbumIds` narrows both ways at once).
- Ids must be 32 hex characters or a dashed GUID. **Anything else is a 422.** Jellyfin
  discards an unparseable list *in full* and answers with the unfiltered library — measured:
  `ArtistIds=abc` returns all 502 albums where a valid id returns 2 — so passing one through
  would turn a narrowing into a widening, on a selection that feeds a write.
- A facet that cannot apply to the media type is a **422**, not a zero.
  `ArtistIds` with `kind=artist` returns 0 of 639 on the server, which reads as an empty
  library rather than an impossible filter.
- Patterns are **literal**: `live` matches exactly `live`, and `*live*` is what matches a
  substring. They are matched against all three labels, so `Various Artists` excludes
  compilations even though that string appears in no track or album name.
- When either scan-backed filter is used, the response carries `scan`:
  `{scanned, matched, excluded, truncated, limit}`. `excluded` and `matched` are disjoint —
  exclusion is evaluated first — and `truncated` means the read stopped at the cap, so
  `total` is a lower bound.
- A selection that needs a scan **over-fetches to fill its limit**: a batch asking for 25
  items keeps reading until it has 25 that pass, rather than returning however many of the
  first 25 survived. `excluded` on the job summary is what explains a short result.

## Editing

| Endpoint | Meaning |
|---|---|
| `POST /api/items/{id}/candidates` | Archived Last.fm entities matching this item, scored. Reads only. |
| `POST /api/items/{id}/diff` | The exact changes applying a candidate would make. **Writes nothing.** |
| `POST /api/items/{id}/apply` | Apply a reviewed change set. Requires `confirm: true`. |
| `GET /api/items/{id}/snapshots` | Snapshot history, newest first. |
| `POST /api/snapshots/{id}/revert` | One-click undo. Requires `confirm=true`. |

## Bulk editing

| Endpoint | Meaning |
|---|---|
| `POST /api/bulk/diff` | **SSE.** Diff every item in a selection; returns a `job_id`. Writes nothing. |
| `POST /api/bulk/apply` | **SSE.** Apply a reviewed `job_id`. Requires `confirm: true`. |
| `POST /api/bulk/{batch_id}/revert` | **SSE.** Undo a whole batch. Requires `confirm=true`. |
| `GET /api/bulk/jobs` | Reviewed diffs still available to apply. |

## Genre blacklist and removal

| Endpoint | Meaning |
|---|---|
| `GET /api/settings/genre-blacklist` | The stored entries, the built-in list, the env var, and the merged set actually enforced. |
| `PUT /api/settings/genre-blacklist` | Replace the stored list from `raw` text. A comma-bearing line is reported in `conflicts`, not split. Returns 200 with `needs_review: true` rather than refusing the whole block. |
| `POST /api/settings/genre-blacklist/preview` | What this text would blacklist, and which live library genres each entry matches. Writes nothing. |
| `GET /api/bulk/genres` | The library's genre entities for one `item_kind` — the same list Jellyfin's own filter offers. |
| `POST /api/bulk/remove-genre/diff` | **SSE.** Every item carrying a genre value, with what removing it would do. Returns a `job_id`. Writes nothing. |
| `POST /api/bulk/remove-genre/apply` | **SSE.** Apply a reviewed removal. Requires `confirm: true`. |
| `GET /api/bulk/remove-genre/jobs` | Reviewed removals still available to apply. |

A removal reuses `POST /api/bulk/{batch_id}/revert` — it is a batch like any other.

**Matching is exact and case-insensitive in both features, never a substring.** A blank
`genre` is refused rather than treated as a wildcard, because an empty match would remove
every genre from every selected item. `decompose: true` additionally removes matching
*parts* of a packed value (`Rock, Reggae` → `Rock`); it is off by default and needs a scan,
because Jellyfin cannot filter by part of a value. See ADR 0015.

## Archive

| Endpoint | Meaning |
|---|---|
| `GET /api/archive/stats` | Payload bytes against the cap, partition sizes, derived counts, archive read metrics. |
| `GET /api/archive/entities` | Search the derived layer offline: `kind`, `tag`, `search`, paging. |
| `GET /api/archive/entities/{kind}/{id}` | One derived entity with its tags, and similar artists for an artist. |
| `GET /api/archive/tags` | Every tag across the archive with how many distinct entities carry it. |
| `GET /api/archive/aliases` | Last.fm autocorrect corrections. |
| `POST /api/archive/reindex` | Rebuild the derived layer. **`dry_run` defaults to true** over HTTP. |
| `GET /api/archive/diagnose` | What is archived and what the derivation cannot use. |

## Editing safely

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

## Bulk editing

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
rather than at the end. An unexpected failure is reported as a short reference id
rather than an exception string; the traceback goes to the server log under that same
reference.

## Hardening

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
