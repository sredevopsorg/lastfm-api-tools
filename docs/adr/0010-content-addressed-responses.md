# 0010. Content-address response bodies; log every observation separately

- **Status:** Accepted
- **Date:** 2026-10-06

## Context

An `artist.getInfo` body is a few kilobytes and may be byte-identical for months,
yet the same artist may legitimately be requested two hundred times. Storing a
full body per request would waste roughly an order of magnitude in space, and it
is the same body every time.

## Decision

Split raw storage in two:

- `lastfm_response` is content-addressed: the primary key is the SHA-256 of the
  canonical JSON body (sorted keys, compact separators). An identical body is
  stored once; repeats bump `last_seen_at` and `observation_count`.
- `lastfm_request` logs one row per HTTP attempt, referencing `response_id` when
  a body was received. It is partitioned monthly by `requested_at` and truncated
  it is never: history is the point.

Canonicalisation happens *before* hashing, so key order, whitespace and
irrelevant request params cannot create spurious duplicates.

## Consequences

- A poll that returns unchanged data costs a ~200-byte observation row instead of
  a duplicate payload.
- The observation log still answers "when did this change, and when did we last
  see the old value", because `first_seen_at`/`last_seen_at` plus the request
  timeline reconstruct it.
- Deletion is protected: the foreign key from `lastfm_request.response_id` uses
  `ON DELETE RESTRICT`, so a response cannot be pruned while observations refer
  to it.
- Hashing and canonicalisation are pure functions with their own unit tests.

## Alternatives rejected

- **One row per request with the body inline.** Rejected: roughly 10x the storage
  for zero extra information.
- **Deduplicate by (method, params) instead of by body.** Rejected: different
  params legitimately yield the same body (name vs. MBID lookups, autocorrect
  variants), and identical params legitimately yield different bodies over time.
