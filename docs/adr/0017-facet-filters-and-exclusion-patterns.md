# 0017. Facet filters are id-validated; exclusion patterns are literal globs

Date: 2026-10-08

## Status

Accepted

## Context

Operators needed to point at a precise set before that set fed a write: "ABBA's albums",
"these three artists' songs in this one album", "everything except the live records". The
browse screen offered a search box, a sort and a `missing` filter, and nothing that could
express any of those three.

Two of them are cheap and one is not, and the difference is not obvious from the UI:

- **Artist and album narrowing is free.** `ArtistIds` and `AlbumIds` are Jellyfin's own
  query parameters. Verified on 12.2.0: `ArtistIds=<ABBA>` returns 2 of 502 albums and 21
  of 5,442 songs, `AlbumIds=<Gold>` returns 19 songs, and the two together return 19 --
  they **union** within a parameter and **intersect** across them. `total` is the filtered
  count, so paging is over the filtered set.
- **Exclusion is not.** Jellyfin has no name-pattern parameter at all. `searchTerm` is a
  substring match on the item's name, cannot be negated, and cannot see the album artist.
  So an exclusion has to read the items, which means a scan, which means the count can be a
  lower bound and has to say so.

Two hazards showed up while probing, and both are the kind that produce a *plausible*
screen rather than an error:

1. **An unparseable id list is discarded in full.** `ArtistIds=abc`,
   `ArtistIds=<32 hex>|<32 hex>` and `ArtistIds=<40 hex>` each returned the **entire
   library** -- 502 albums and 5,442 songs -- because nothing in the list parsed and the
   parameter was ignored. A junk entry *alongside* a valid one is dropped individually, so
   `ArtistIds=<junk>,<ABBA>` correctly returns ABBA's 2. The failure mode is therefore a
   filter that silently does the opposite of filtering, and on these screens the result
   feeds a write.
2. **A facet that cannot apply returns zero, not an error.** `ArtistIds` on
   `IncludeItemTypes=MusicArtist` returns 0 of 639 albums' worth of artists, and `AlbumIds`
   on a music album does the same. An empty table reads as "this library has nothing in
   it" rather than "that filter cannot work here".

## Decision

**Ids are shape-checked at the boundary, and an unusable one is refused with a 422.**
`domain.identifiers` accepts exactly the two spellings Jellyfin parses -- 32 hex
characters, or the dashed GUID form, either case, whitespace tolerated -- and refuses
anything else, naming the field and the value. The message states the reason, because an
operator looking at a "filter" that widened their selection needs to know why.

Dropping the bad id instead was rejected: dropping an entry *narrows* a union, so the
caller would get results and believe they were the ones asked for. Refusing to answer is
the only outcome that cannot be mistaken for success.

The SPA takes the opposite approach at its own boundary, and deliberately: it drops
non-GUID ids out of the URL rather than sending them, exactly as it already falls back for
an unrecognised `sort`. A URL is a link someone may have edited, not a contract, and
erroring a screen over a stale link is worse than showing the unfiltered list. The API
still refuses, so a hand-written request cannot bypass the guard.

**A facet that cannot apply to the media type is refused rather than answered with zero.**
An empty table that looks like missing data is the class of defect this project treats as
a bug, so `facet_filters` raises instead.

**Exclusion patterns are literal, case-insensitive globs matched against the item's name,
its album and its album artist.** Three parts to that:

- *Glob, not regex.* The input is typed into a box by hand. A regex box is a
  denial-of-service surface aimed at the operator's own server, and `fnmatch` escapes
  everything except `*` and `?`, so that is structural rather than a convention.
- *Literal, with no implicit wrapping.* `live` matches exactly `live`; `*live*` is what
  matches a substring. The friendlier-looking alternative is how `Rock` comes to swallow
  `Rockabilly` one field over (ADR 0015), and the same mistake here would quietly drop
  items nobody meant to drop.
- *All three labels.* `Various Artists` is a compilation marker that lives in the album
  artist and appears in no track or album name, so a matcher reading only `Name` would
  leave every compilation in a selection that feeds a write.

**Exclusion is evaluated before the missing-aspect test, in one shared pass.** This keeps
`excluded` and `matched` disjoint, so the report can name either without double-counting an
item. Reporting `scanned - matched` instead would call an item dropped by a pattern
"missing genres", which is a different fact about the library.

**A filtered selection over-fetches to fill its limit.** A batch of 25 that drops 21 items
to a pattern would otherwise return four, and the operator would read that as "there is
nothing else to do". The selection keeps reading until it has `limit` items that pass, the
library ends, or the scan cap is reached -- and reports what it read.

**Exclusion narrows a view or a selection; it never deletes.** A pattern that hides an
album from the browse screen does not remove a genre from it. Deleting is the separate,
reviewed, revertible removal tool (ADR 0016's sibling feature).

## Consequences

- A typo in an id is a 422 rather than a library-wide batch. The cost is that a client
  cannot pass an opaque id through without knowing its shape; the benefit is that the
  silent-widening failure cannot happen.
- Every selection surface shares one set of decisions: `clean_item_ids`, `compile_patterns`
  and `facet_filters` for what is safe to send, `service.labels` for which strings a pattern
  sees, and `is_missing`/`is_excluded` for the predicates. The three *write* paths -- batch
  diff, genre removal, harvest -- additionally go through `service.selection.Selection` and
  `collect`, which is where the over-fetch and the counters live.

  The browse route composes the same primitives itself rather than going through `Selection`,
  because it *windows* the scan and reports what it read instead of collecting a signature.
  That is a real asymmetry and worth stating rather than papering over: the browse and the
  write paths share the rules, not the traversal. A change to what "excluded" means belongs
  in the primitives, where both see it; a change to how a selection is collected belongs to
  whichever of the two is being changed.
- A browse with no patterns and no aspects stays a single Jellyfin request. Routing it
  through the scanner would multiply the request count for no gain, so `needs_read` gates
  it explicitly.
- Exclusion costs a scan bounded by `MAX_SCAN_ITEMS` (2,000, tunable via
  `METAEDIT_BROWSE_SCAN_CAP`), and `scan.truncated` says when the bound was hit. The
  response never presents a lower bound as a total.
- `METAEDIT_BROWSE_SCAN_CAP` was a test hook and is now an operator knob: exclusion makes
  scans routine rather than exceptional.

## Alternatives rejected

- **Let Jellyfin validate the ids.** It does not: it silently ignores what it cannot parse.
  There is no error to rely on.
- **Send the ids as repeated parameters.** The server unions them, so it would work -- and
  would be a second spelling of the same thing, which is one more way to be wrong. The
  comma-joined form matches what a live probe used.
- **Substring matching for patterns.** Friendlier for a one-word pattern and unsound for
  the reason above; a wildcard is one character away and says what it means.
- **A regex input.** More expressive, and the expressiveness is aimed at the server that
  runs it.
- **Refusing an empty pattern list vs. treating it as "match nothing".** Neither: an empty
  list is the default and excludes nothing. A *blank entry* is dropped rather than refused,
  because a blank line in a textarea is not an instruction.
- **Persisting patterns as a stored policy.** A per-query filter kept in the URL is what
  the other browse parameters are, and a stored version would need the blacklist's
  storage, review and precedence machinery (ADR 0016) for a different problem.
