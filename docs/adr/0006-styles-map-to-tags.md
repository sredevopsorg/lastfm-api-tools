# 0006. Map Last.fm styles onto `BaseItemDto.Tags`; genres onto `Genres`

- **Status:** Accepted
- **Date:** 2026-10-06

## Context

The Last.fm API exposes one flat set of community tags per artist, album and
track, each with a popularity count. It has no genre/style distinction. Jellyfin
items have both a `Genres` array and a `Tags` array; only `Genres` gets
genre-entity treatment and navigation.

## Decision

Treat "genre" and "style" as *our* mapping concepts over the single Last.fm tag
set:

- The highest-ranked `LASTFM_GENRE_LIMIT` tags become `Genres`.
- The next `LASTFM_STYLE_LIMIT` tags become `Tags` (the style bucket).
- Both are merged with existing values rather than replacing them, by default.

The split points, the minimum tag count, the blacklist and the caps are all
configuration, never hard-coded.

## Consequences

- The mapping is honest about what Last.fm provides and fully tunable without
  re-fetching anything, because the raw archive is retained (ADR 0008/0009).
- Retuning the split is a reindex, not a re-crawl.
- Genre entity creation in Jellyfin is a server concern; whether a new genre
  appears immediately or on the next library scan is recorded in the README as
  observed behaviour rather than assumed.

## Alternatives rejected

- **Everything into `Genres`.** Rejected: it floods genre navigation with
  descriptive noise such as `seen live` or `british`.
- **Everything into `Tags`.** Rejected: Jellyfin clients then show no genres at
  all for music, which is the primary thing users filter by.
- **Guessing genres from a bundled genre list.** Rejected: an opinionated
  classifier with its own maintenance burden, for a problem the operator can
  solve by tuning two integers.
