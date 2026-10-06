# 0007. Optimistic concurrency on `Etag`/`DateLastSaved`

- **Status:** Accepted
- **Date:** 2026-10-06

## Context

Jellyfin owns the item data; we cannot take a database lock on it. A user may
edit an item in Jellyfin's own metadata editor between our read (which builds the
full-overwrite payload) and our write. Because our body is a full overwrite, a
stale read would silently revert their change.

## Decision

Capture the item's `Etag` (and `DateLastSaved`) at read time, present it with the
diff, and require it to match immediately before the write. On mismatch, return
`409 Conflict` with both the current and the expected state, and let the user
review and force-apply.

If `Etag` turns out not to change after `UpdateItem`, fall back to a hash of the
whitelist fields combined with `DateLastSaved`. Which of the two works is an open
item verified against a live server and recorded in the README.

## Consequences

- The window for a silent clobber is reduced from "however long the user takes to
  review a diff" to a single round trip.
- It is best-effort, not transactional. For a single-operator tool that is the
  right trade; the cost of being wrong is one item's metadata, which ADR 0004
  makes reversible.
- Clients must pass `expected_etag` and handle `409`.

## Alternatives rejected

- **No concurrency control.** Rejected: `UpdateItem`'s full-overwrite semantics
  make a lost update both silent and total.
- **A lock table on our side.** Rejected: it cannot prevent edits made in
  Jellyfin itself, so it would be theatre.
