# Phase 3 design: derivation and `reindex`

- **Status:** Specification — not yet implemented (`metaedit reindex` raises `NotImplementedError`)
- **Implements:** ADR 0009 (raw archive is the source of truth), ADR 0010 (content addressing)
- **Depends on:** the raw layer and `ArchiveStore` from phase 2

This document fixes the contract the derived tables must satisfy. ADR 0008 lists
them and ADR 0009 requires them to be reproducible from the raw layer alone, but
neither states the algorithm, the identity rules, or the swap procedure. Phase 3
must not start without them settled, because a derived layer whose identity rules
are improvised cannot be rebuilt reproducibly — which is the entire point.

## 1. The invariant to satisfy

> Given the same `lastfm_request` + `lastfm_response` rows, reindexing must
> produce byte-identical derived rows, with the network unreachable.

Consequences, all of which are testable:

1. **No network access.** `reindex` must never construct a `LastfmClient`.
2. **No wall-clock, no randomness in derived values.** Every derived timestamp
   comes from the raw rows (`requested_at`, `first_seen_at`), never `now()`.
   A reindex run twice with `now()` in the middle must produce identical output.
3. **No dependence on insertion order.** Derived order must come from an explicit
   `ORDER BY` over raw columns, not from physical row order.
4. **Deterministic entity ids.** Derived ids are assigned by a deterministic
   `ORDER BY`, so a full rebuild reproduces the same numbers.

Rule 2 is the one that is easy to violate accidentally and impossible to notice
later: a `now()` in `last_seen_at` would make the store unreproducible while
looking perfectly healthy.

## 2. Identity rules

`lastfm_artist.identity`, `lastfm_album.identity` and `lastfm_track.identity` are
`text` with a unique constraint per table. The identity is the **MBID when Last.fm
supplied one, else a name key**:

| Table | With MBID | Without MBID |
|---|---|---|
| `lastfm_artist` | `mbid:<mbid>` | `name:<escape(name_norm)>` |
| `lastfm_album` | `mbid:<mbid>` | `album:<escape(artist_name_norm)>\x1f<escape(name_norm)>` |
| `lastfm_track` | `mbid:<mbid>` | `track:<escape(artist_name_norm)>\x1f<escape(name_norm)>` |

`\x1f` is the ASCII unit separator, chosen as the field delimiter.

`escape` percent-encodes `%` and `\x1f` in each component before joining:

```
escape(s) = s.replace("%", "%25").replace("\x1f", "%1F")
```

The escaping is not optional. Last.fm names and tags are user-generated text, so
a control character *can* appear in one — the claim that `\x1f` "cannot appear in
a name" is not something Last.fm guarantees. Without escaping, an artist literally
named `"A\x1fB"` would forge the same key as the pair `("A", "B")` and merge two
unrelated entities. Encoding `%` first keeps the mapping injective, so distinct
component pairs always produce distinct keys. The same rule applies wherever a
composite key is built from Last.fm text.

`name_norm` is the canonicalisation from `adapters/lastfm/canonical.py` (NFC,
whitespace collapsed, case preserved). Case is preserved deliberately: Last.fm
echoes its own canonical spelling, and folding case here would merge distinct
entities in the derived layer while the raw layer kept them apart.

### The MBID-arrival problem, and how resolution handles it

Identity is *derived from the data*, so it changes when Last.fm starts returning
an MBID for an entity we previously only knew by name. Naively that yields two
rows — a `name:` row and an `mbid:` row — for one artist, and the older one keeps
winning on `last_seen_at`.

Resolution, in one deterministic pass:

1. Group all observations for a table by their **resolved identity**: MBID when
   present, else the name key. Process groups in `ORDER BY identity`.
2. For each group, collect the candidate name keys it could be known by
   (`name:<name_norm>` plus every observed `name_norm`).
3. If a group carries an MBID, **absorb** any name-keyed group whose name key is
   in that candidate set: the MBID group wins, and the absorbed group's
   observations join it.
4. Materialise one row per surviving group.

This makes the operation a pure function of the raw rows rather than of the order
in which they arrived. The absorbed row's observations are not lost — they are
already in the raw layer, which is what makes the merge safe to repeat.

## 3. Field selection within an entity

An entity accumulates observations over time, and later responses are not
guaranteed to be richer than earlier ones (a truncated bio, a dropped tag list).
For each entity row:

- `first_seen_at` = `min(requested_at)` over its observations. Never moves.
- `last_seen_at` = `max(requested_at)`. Moves forward only.
- `latest_response_id` = the `response_id` of the newest **non-error** observation.
  It points into `lastfm_response`, satisfying ADR 0009's provenance requirement.
- Parsed columns (`name`, `mbid`, `url`, `listeners`, `playcount`, `overview`,
  `releasedate`, `production_year`, images, …) come from that same newest
  non-error observation.
- A field that is `None` in the newest observation **overwrites** a previously
  non-null value. This is intentional: Last.fm genuinely removes data, and the
  archive's job is to record what is currently true. The previous value remains
  recoverable from the raw layer, which is the reason we kept it.
- Error observations (codes 6/7, `is_error = true`) never contribute fields. They
  contribute only to the timeline, and one still updates `last_seen_at` — that is
  how "this artist disappeared from Last.fm on <date>" becomes answerable.

## 4. Tag edges

`lastfm_tag_edge` is **one row per (entity, tag)**, replaced wholesale per entity
on each derivation — it is a snapshot of the entity's current tags, not a log.
The log is the raw layer.

- `entity_kind` ∈ `artist` | `album` | `track`; `entity_id` is the derived row id.
- `tag_name` preserves Last.fm's spelling; `tag_name_norm` is the canonical form
  used by the uniqueness constraint.
- `rank` is the position in the source list (0-based, ascending = most popular).
- `count` is populated **only** from `artist.getTopTags`, the one method that
  returns popularity counts. It is `NULL` elsewhere, never fabricated.
- Ordering when no counts exist falls back to list order, which is itself a
  ranking signal for the `*getInfo` tag arrays.
- `observed_at` is the source observation's `requested_at`, not `now()`.

Because `prefix\0` never appears in a tag, `(entity_kind, entity_id, tag_name_norm)`
is a safe unique key.

## 5. `lastfm_entity_tag`

Its purpose was never stated, which is exactly the kind of thing that rots. It is
an **aggregate, recomputed by reindex and written only by reindex**:

> One row per distinct tag across the whole archive, with `entity_count` = the
> number of distinct `(entity_kind, entity_id)` pairs carrying it.

It exists so archive-wide questions ("which of my 400 artists share `shoegaze`?")
are answerable without a scan of `lastfm_tag_edge`, which is the table that will
grow without bound. It is never read to decide a metadata change — the per-entity
tag policy reads `lastfm_tag_edge` and the raw tag list. That separation matters:
an aggregate must never become an input to a write.

## 6. Similarity and aliases

`lastfm_similarity` — one row per `(artist_id, peer_name_norm)`, replaced per
artist on each derivation, `rank` and `match` taken verbatim from
`artist.getSimilar`. `peer_artist_id` is backfilled **only** when the peer itself
exists in `lastfm_artist`; it is never invented, and a `NULL` there is normal.

`lastfm_artist_alias` — one row per observed autocorrect: `requested_name_norm`
→ `canonical_artist_id`. Derived from `Artist.corrected` on `artist.getInfo`
responses where the corrected name differs from the requested one. Like
`lastfm_entity_tag`, it is written only by reindex; its reader is the phase 2
lookup path, which uses it to skip a network round trip on a known misspelling.

## 7. Reindex algorithm

```
metaedit reindex [--dry-run] [--since <ISO date>] [--only artist|album|track|tag|similar|alias]
```

**Full run**

1. Derive in memory / into staging tables, in a single transaction:
   a. artist groups → `lastfm_artist_staging`
   b. album groups → `lastfm_album_staging`
   c. track groups → `lastfm_track_staging`
   d. tag edges from the newest non-error observation of each staged entity
   e. similarity from `artist.getsimilar` observations
   f. aliases from `artist.getinfo` autocorrect observations
   g. `lastfm_entity_tag` aggregate from (d)
2. Validate: every `latest_response_id` exists in `lastfm_response`; every
   `lastfm_tag_edge.entity_id` resolves to a staged entity. Any failure aborts the
   whole run with the offending rows named.
3. Swap each table with `ALTER TABLE ... RENAME` inside one transaction. Staging
   tables are created with the same column definitions as their targets, so the
   swap is metadata-only and effectively instant; the old tables are dropped
   afterwards.
4. Record the run: watermark, row counts, duration → `/api/archive/stats`.

**`--dry-run`** performs steps 1 and 2, then compares staging against the live
tables row by row and reports `{added, removed, changed}` per table with a sample
of differing rows. It writes nothing. This is the reviewer's tool for a model or
policy change, and it is the assertion form of the §1 invariant.

**`--since` / `--only`** are the incremental path: restrict to entities touched by
observations newer than the watermark (or to one table), recompute those entities,
delete their existing `lastfm_tag_edge` / `lastfm_similarity` rows, and reinsert.
Incremental and full runs must agree; a test asserts exactly that, because a
divergence here would silently corrupt the derived layer over time.

## 8. What reindex must NOT do

- **Never touch the raw layer.** `lastfm_request` and `lastfm_response` are
  append-only. Not one row is updated or deleted.

  `lastfm_request` records **HTTP attempts to Last.fm**, and only those. An
  archive read is not an attempt and must never be written there; reads are
  counted in process memory instead. A derived value that consumes request rows
  must therefore treat every row as a real network call.
- **Never touch `snapshot` or `audit_log`.** Those record what we wrote to
  Jellyfin and are not derived from Last.fm at all.
- **Never call the network.** See §1. The only exception in the whole system is
  the client's own fetch path, which is not on this code path.
- **Never enforce or prune the storage cap.** The cap is measured by
  `archive/stats.py` and acted on by an explicit operator command (ADR 0011).
- **Never write a derived value without a `latest_response_id`.**

## 9. Acceptance criteria for phase 3

1. Wiping every derived table and running `reindex` with the network hard-blocked
   reproduces them byte-for-byte — twice in a row, and across a fresh database.
2. `reindex --dry-run` on an unchanged archive reports zero differences.
3. `reindex --since` agrees with a full `reindex` on the same input.
4. `lastfm_entity_tag.entity_count` equals the number of distinct entities per tag
   in `lastfm_tag_edge`.
5. An entity that gains an MBID later appears as **one** row, not two, and a test
   covers exactly that regression.
6. No derived column is populated from `now()`; a test asserts that two runs
   separated by a clock change produce identical output.
