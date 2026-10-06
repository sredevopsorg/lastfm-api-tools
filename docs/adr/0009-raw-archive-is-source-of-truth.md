# 0009. The raw archive is the source of truth; derived data is rebuildable

- **Status:** Accepted
- **Date:** 2026-10-06

## Context

Once responses are archived (ADR 0008), the obvious next step is to store only
the fields we currently care about. But the mapping is the most likely thing to
change: the genre/style split will be tuned, the confidence model will be
adjusted, new fields will be added, and bad parses will need fixing.

## Decision

Nothing derived is ever written without a reference to the raw response it came
from. Every entity, tag edge and similarity row carries a `latest_response_id`.
`metaedit reindex` re-derives the entire derived layer from the raw layer alone,
with no network access, into staging tables that are swapped in atomically.
`--dry-run` reports the row-level delta instead of writing.

This is a tested property, not an aspiration: rebuilding from the raw archive
with the network hard-blocked must reproduce the derived tables byte-for-byte.

## Consequences

- Model and policy changes are cheap, offline and reversible; a bad parse is
  fixed by reindexing, not by re-crawling.
- The database holds some redundancy (raw JSON plus parsed columns). That is a
  deliberate trade of disk for the ability to correct the past.
- Reindexing is a real operation with a real duration, so it must be observable
  and idempotent; it reports counts and runs in one transaction.
- Derived tables become a cache with a well-defined invalidation story, which is
  exactly what a cache should be.

## Alternatives rejected

- **Store derived only.** Rejected: it makes the data hostage to today's parsing
  code, and any mapping bug becomes permanent data loss.
- **Store raw only, parse per request.** Rejected: every page load would re-parse
  and re-rank, and the offline query features (tag co-occurrence, change
  detection) would be impossible to write.
