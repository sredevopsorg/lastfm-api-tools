# 0004. Snapshot the writable fields before every write; revert replays it

- **Status:** Accepted
- **Date:** 2026-10-06

## Context

A metadata write is destructive by construction (ADR 0003): the body we send
becomes the item's state. A wrong mapping — a bad Last.fm match, an over-eager
tag policy — would otherwise overwrite curated genres, tags and overviews with
no way back.

## Decision

Before every write, persist an immutable snapshot of the item's current
`WRITABLE_FIELDS` values, plus the `Etag` and `DateLastSaved` observed at read
time. Revert is a normal write built from the snapshot's fields, and is itself
recorded as a new audit row.

Snapshots are never mutated or deleted. `source_op` distinguishes
`apply` / `revert` / `bulk_apply`, and `batch_id` groups a bulk operation so a
whole run can be rolled back.

## Consequences

- One-click undo with byte-accurate field values.
- One extra table and two endpoints; the storage is trivial (one row per write).
- Reverting is not "restore the file" — it is a forward write, so the resulting
  state is a new revision. Jellyfin's own history (if any) sees both operations,
  which is honest about what happened.
- Every write attempt, including failures, is recorded in `audit_log` with the
  `lastfm_request_ids` that justified it, so provenance is traceable from a
  written value back to the archived response it came from.

## Alternatives rejected

- **Keep a JSON file per item on disk.** Rejected: a second persistence mechanism
  with its own backup story, for data that belongs next to the audit log.
- **Compute a reverse patch on demand.** Rejected: it cannot be computed after
  the fact, which is exactly when it is needed.
