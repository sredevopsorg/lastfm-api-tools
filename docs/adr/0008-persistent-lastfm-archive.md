# 0008. Persist every Last.fm request and response in a permanent archive

- **Status:** Accepted
- **Date:** 2026-10-06

## Context

The Last.fm API is rate limited (around 5 requests/second per originating IP,
with suspension for sustained excess) and its Terms of Service cap stored
Last.fm Data at 100 MB. The obvious implementation is a short-TTL cache.

## Decision

Keep a persistent, incremental, structured local archive of *every* response we
receive, not a TTL cache. Three layers:

1. **Raw observations** — `lastfm_request`: one append-only row per HTTP attempt,
   success or failure, with canonicalised params and the Last.fm error code.
   `lastfm_response`: the response bodies, content-addressed.
2. **Derived current state** — `lastfm_artist` / `lastfm_album` /
   `lastfm_track`, one row per entity, each carrying the `latest_response_id` it
   came from.
3. **Derived graph** — `lastfm_tag_edge`, `lastfm_similarity`,
   `lastfm_artist_alias`, `lastfm_entity_tag`.

The editor reads through the archive first: a fresh stored response is served
without any network call, and the UI shows an "offline" badge when that happens.

## Consequences

- A response is never paid for twice while it is still fresh, and the request
  budget is spent only on genuinely new or stale data.
- We can answer questions offline that were never anticipated: which tags our
  artists share, what changed on Last.fm since a date, when an entity disappeared.
- Failed lookups (error codes 6/7) are archived too, so "this artist is not on
  Last.fm" becomes a dated observation rather than an unanswerable question.
- Cost: disk, plus one extra indirection layer, plus the obligation to keep the
  derived layer rebuildable (ADR 0009) and the byte budget visible (ADR 0011).
- Growing the archive is incremental by construction: every new request enriches
  it without a separate import step.

## Alternatives rejected

- **TTL cache only.** Rejected: it discards the only copy of data we already
  spent a rate-limited request on, and makes any future offline use impossible.
- **Offline bulk import of a Last.fm dump.** Not available; the API is the only
  source, which is exactly why retaining what we fetch matters.
- **Storing raw payloads in object storage.** Rejected: a second backing service
  for a dataset that fits comfortably in Postgres and is naturally relational.
