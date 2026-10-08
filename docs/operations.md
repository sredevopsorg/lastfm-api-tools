# Operations

Running `metaedit` day to day: what the archive stores, the CLI that tends it, and
what was learned by pointing it at real servers.

## The archive

Every Last.fm request is stored, in three layers:

1. **Raw** — `lastfm_request` logs one row per HTTP attempt (success *or*
   failure) in a monthly partition; `lastfm_response` stores response bodies
   content-addressed by body hash, so an unchanged body is stored once while
   every observation of it is still recorded.
2. **Derived current state** — `lastfm_artist` / `lastfm_album` / `lastfm_track`,
   one row per entity, each pointing at the `latest_response_id` it came from.
3. **Derived graph** — `lastfm_tag_edge`, `lastfm_similarity`,
   `lastfm_artist_alias`, `lastfm_entity_tag`.

Layers 2 and 3 are reproducible from layer 1 alone, with no network access.

`lastfm_request` logs **HTTP attempts to Last.fm only**. A lookup answered from the
archive is the case where no attempt was made, so it is not written there; those
reads are counted in process memory and reported by `GET /api/archive/stats` under
`reads`, alongside `stray_archive_reads`, which counts any historical rows written
before that distinction was enforced.

## CLI

```bash
uv run metaedit archive-stats                    # bytes stored vs. the cap
uv run metaedit partitions --months 6            # extend the partition window
uv run metaedit prune-raw --keep-days 365        # report; add --yes to actually drop
uv run metaedit reindex                          # rebuild the derived layer
uv run metaedit reindex --dry-run                # what a rebuild would change
uv run metaedit reindex --only artist            # one table family
uv run metaedit archive-entities                 # what is archived, and what is not understood
```

`reindex` derives the structured layer from the raw archive: entity rows
for artists, albums and tracks, tag edges with real popularity counts, similar
artists, autocorrect aliases and an archive-wide tag aggregate. It is reproducible
by construction — every derived primary key is assigned deterministically, never by
a sequence — and `--dry-run` reports the delta without writing.

`reindex` needs no network access and never writes to the raw layer, so it is safe
to run at any time, and safe to interrupt: the swap is one transaction, so a failure
leaves the previous derived tables untouched. Its `--dry-run` is the reviewer's tool
for a parsing or policy change.

Because the failure mode of a parsing mismatch is *absence* — fewer entities, with
every count downstream still looking plausible — the report carries a
`unexpected_shapes` count. **Zero is healthy.** A rise means archived bodies are no
longer understood, and `metaedit archive-entities` names the offending method and
payload keys.

The contract it implements is fixed in
[`docs/design/0003-derivation-and-reindex.md`](design/0003-derivation-and-reindex.md).

Known deviation: `reindex --since` parses and validates its timestamp but performs a
full rebuild rather than a partial derivation. Recorded in the same design doc §7
with the reasoning; it cannot produce wrong data, only cost time.

## Retention and the ToS cap

**Nothing is deleted automatically.** The Last.fm API Terms of Service cap stored
Last.fm Data at 100 MB; that number is measured and displayed, and pruning is
always an explicit operator decision (`prune-raw` reports what it would drop and
refuses to act without `--yes`).

Retention is monthly, because partitions are: `prune-raw --keep-days N` lists
only whole months that are entirely older than the window, and deliberately keeps
the cutoff month and the month before it too. A shallow window therefore reports
nothing rather than risking the loss of recent observations.

## Live-verified behaviour

Both adapter suites have been run against real services (a self-hosted Jellyfin
12.2.0, and the live Last.fm API). Findings that contradicted the code, kept because
each one is a trap that a future change could walk back into:

**Jellyfin**

- **Authentication is the `MediaBrowser` scheme**, not a bare key:
  `Authorization: MediaBrowser Client="…", Device="…", DeviceId="…", Version="…", Token="…"`.
  Jellyfin 12 **disabled the legacy channels**, so `Authorization: <key>`,
  `X-Emby-Token` and `?api_key=` all return 401. A bare token without the scheme is
  parsed as a malformed scheme and returns 400.
- **An API key is userless**, so `GET /Users/Me` answers **400** — by design. The
  body is a generic RFC9110 ProblemDetails that does *not* name the reason, so
  elevation is confirmed by a follow-up authenticated read instead. API keys carry
  administrator privileges, which is what an item update needs.
- **`/Artists` must not be given `includeItemTypes`.** Passing
  `includeItemTypes=MusicArtist` returned **0** results while a bare call returned
  **594**. A filter that looks harmless silently emptied the browse.
- **`/Library/MediaFolders` omits `ItemId`** for every library, so there was no id to
  scope a browse by. `/Library/VirtualFolders` supplies it.
- **`Etag` exists but only when requested via `fields=`.** Concluding otherwise would
  have sent phase 5 down an unnecessary fallback. `DateLastSaved` is *not* returned
  for music items even when requested, so `Etag` is the version token for ADR 0007.
- **`GET /Items/{itemId}` requires a user context.** With a userless API key it returns
  **400** ("Error processing request.") for *every* id — including one the server itself
  just returned from `/Artists` — unless `userId` is supplied. The list endpoints
  (`/Items?ids=…`) tolerate its absence, which is what made this easy to miss: every
  list-based test passed while the entire single-item path was broken. The contract
  lists `userId` as *optional*, so a schema check agrees with the broken assumption.

  This mattered a great deal, because apply **reads the item first**: the failure
  surfaced as `Jellyfin rejected the request to /Items/… with 400` and no write was ever
  attempted. The client now discovers a user (an explicit `JELLYFIN_USER_ID` wins,
  otherwise the first administrator in sorted-id order is cached per process).
- Item type names (`MusicArtist`, `MusicAlbum`, `Audio`) are correct — 639 artists,
  502 albums, 5442 songs on that server.
- Every requested `fields=` value is honoured **except `ParentId`**, which is
  returned anyway as part of the base DTO on some endpoints. No whitelist field is
  omitted, so a read can round-trip the write payload.

**Last.fm**

- `artist.getTopTags` **does** carry `count` (10/10 tags). It is also **envelope-free**
  (`{"toptags": ...}`, no `{"artist": ...}` wrapper), so the entity derivation skipped
  it entirely and every `lastfm_tag_edge.count` was null — which silently disabled
  `TagPolicy.min_count` and reduced genre ranking to list order everywhere. The counts
  are now folded into the artist entity *and* its tag edges; applying them only to the
  edges would have looked correct while still filtering nothing, because the mapping
  layer reads the entity. Album and track tag lists carry no counts anywhere in the API,
  so their ranking stays list order by design rather than by accident.
- `artist.getsimilar` puts the **peer list** under `artist` and loses the owning
  artist's name, confirming the owner must come from the request params.
- `album.getInfo` returns tags under **`tags`** (no counts) and leaves `toptags`
  absent, so album tag ranking falls back to list order.
- `album.getInfo` **does** return a usable `wiki.summary`. The phase 3 code asserted
  the opposite and discarded it, so **album overviews were silently unavailable** —
  fixed by migration `0002_album_overview`.
- `album.getInfo` returns **no `releasedate`**, by name or by MBID, so album years
  cannot be sourced from it. The column is retained for tolerance and stays null.

## Operations notes

- The app port is bound to `127.0.0.1` by default: it holds an administrator Jellyfin
  key and has no authentication of its own. Put an authenticating proxy in front of it
  before exposing it anywhere.
- `ARCHIVE_ENABLED=false` stops recording to the archive and skips partition
  maintenance at startup. Everything that reads the archive then has nothing to read,
  so the editor finds no candidates and `reindex` has nothing to derive from. It exists
  as an escape hatch, not as a supported configuration.
- `ARCHIVE_SOFT_CAP_BYTES` is a *soft* cap, and the accounting is per *distinct payload*:
  re-observing an already-stored body costs nothing and is always allowed, so the cap only
  ever blocks genuinely new data. Crossing `ARCHIVE_WARN_RATIO` of it warns; crossing
  `ARCHIVE_REFUSE_RATIO` makes new payloads fail with `ArchiveCapReached`. `prune-raw` is
  the only thing that reduces the number.
- **Never apply an autogenerated Alembic revision here without reading it.**
  `alembic revision --autogenerate` emits `op.drop_table` for every
  `lastfm_request_YYYY_MM` partition, because those are created at runtime by
  `metaedit.db.partitions` rather than declared on the model, so the comparison reads them
  as tables to remove. Applying that output verbatim deletes the archived request log. For
  the same reason `alembic check` cannot be used as a CI gate: its report is
  indistinguishable from real drift. `0003_genre_blacklist` records this in its docstring.
- The genre blacklist lives in the `genre_blacklist` table and is edited at
  Settings → *Blacklisted genres*. It is part of a database backup, unlike
  `TAG_BLACKLIST_EXTRA`, which is part of the deployment. Both apply, and the two are
  merged; the UI reports how many entries came from each source.
- Blacklisting a genre stops it being *proposed*, and never rewrites existing items.
  Removing a stored genre is the separate *Remove genre* screen, which is reviewed and
  revertible like any other batch.
- `METAEDIT_BROWSE_SCAN_CAP` bounds how many items a filter that needs a read will look
  at, and defaults to 2,000. Two filters need one: `missing`, which Jellyfin cannot express
  for music, and `exclude`, which it cannot express at all. The response reports
  `scanned`, `matched`, `excluded` and `truncated` under `scan`, and `truncated: true` means
  the filtered count is a *lower bound* rather than a total -- which the browse screen says
  in words. It began as a test hook for the truncation path and is now a real knob: with
  exclusion patterns in routine use, a scan is routine too. Raise it on a large library and
  the filter gets slower rather than wrong, which is the direction to prefer.
- Artist and album filters (`artist_ids`, `album_ids`) are **free** -- they are Jellyfin's
  own parameters, so nothing is scanned and no cap applies. Exclusion patterns are the only
  user-facing filter that costs a read.
- Ids in those filters must be 32 hex characters or a dashed GUID. Jellyfin silently
  discards a list it cannot parse *in full* and answers with the **unfiltered** library --
  measured: `ArtistIds=abc` returns all 502 albums where a valid id returns 2 -- so the API
  refuses anything else with a 422 rather than risk a filter that widens. The SPA drops a
  bad id from the URL instead, so a stale link shows the unfiltered list rather than an
  error page.
