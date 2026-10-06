# 0011. Measure the ToS cap; never silently delete archive data

- **Status:** Accepted
- **Date:** 2026-10-06

## Context

The Last.fm API Terms of Service define a "Reasonable Usage Cap" of 100 MB of
stored Last.fm Data, and require aggressive caching. A tool that keeps a
permanent archive is therefore obliged to keep that number in view.

## Decision

Measure stored payload bytes continuously (`archive_stat`, plus an
`/api/archive/stats` endpoint and a `metaedit archive-stats` command). Warn at
80 % (`ARCHIVE_WARN_RATIO`). At 100 % refuse to store *new distinct* payloads and
return `507 archive_cap_reached` with the exact byte count and the remedy.

Never delete automatically. Deleting archive history is only ever an explicit
operator action (`metaedit prune-raw --keep-days N`, which reports what it would
drop and requires `--yes`).

Repeat observations of an already-stored body are always allowed, because they
cost no new payload bytes.

## Consequences

- The cap can never be exceeded by surprise, and the cost of staying under it is
  a visible number the operator controls.
- The tool does not destroy the operator's own data to satisfy a term that is
  between the operator and Last.fm. If the archive must shrink, the operator
  chooses what to lose.
- Storage growth is bounded in practice anyway, because Last.fm metadata changes
  slowly and content addressing (ADR 0010) collapses repeats.

## Alternatives rejected

- **Automatic LRU eviction at the cap.** Rejected: silently discarding history is
  precisely the failure mode the archive exists to prevent, and it would discard
  the oldest — often the most valuable — observations first.
- **Ignore the cap.** Rejected: the ToS is explicit, and the operator is the
  one who bears the consequence of a suspension.
