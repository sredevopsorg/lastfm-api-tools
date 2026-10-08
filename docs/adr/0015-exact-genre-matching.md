# 0015. Genre matching is exact and case-insensitive; a comma is never guessed at

- **Status:** Accepted
- **Date:** 2026-10-08

## Context

Two features need to answer "is this stored genre the one the operator named?" — the
blacklist, which decides what may be proposed for writing, and the removal tool, which
deletes a value from a library. Both take operator input, and both are destructive when
they answer wrongly.

Three facts about real data, all measured against Jellyfin 12.2.0 and a live 6,583-item
music library, combine badly:

1. **Jellyfin's own `Genres` parameter matches a whole value, case-insensitively.**
   Verified: `Genres=alternative rock` returns 23 artists; `Genres=ALTERNATIVE ROCK` and
   `Genres=AlTeRnAtIvE rOcK` return the same 23; every one of the 123 items returned for
   `Genres=rock` genuinely carries `rock` and not merely a value containing it.

2. **Genre values legitimately contain commas and semicolons.** Measured: 8 of 502 albums
   carry at least one, including `EBM, Synth-pop`, `Rock, Reggae`, and
   `Hip Hop, Rock, Latin, Funk / Soul, Pop, Children's, Folk, World, & Country`. The
   live genre-entity list has 39 entries for artists, including the distinct pairs
   `Dark Wave`/`Darkwave` and `Goth`/`Gothic`.

3. **The natural input format is comma-separated.** The requested shape was literally
   `rock, a blacklisted genre, etc`.

So a comma means "separator" in the request and "part of the name" in the data, and the
same character carries both meanings in the same feature. The difference is not cosmetic:
`Genres=Rock` returns **67** albums where `Genres=Rock, Reggae` returns **1**. An
implementation that treated one as containing the other would delete 67 albums' worth of
curation for a request naming a genre that occurs once.

Additionally, `TagPolicy.classify` runs every Last.fm tag through `SPLIT_PATTERN`
(`,`, `;`, `|`, ` / `), so a *proposed* genre is already a piece, while a *stored* genre
may not be. The two sides of the comparison are not symmetric, and assuming they are is
the specific mistake this record exists to prevent.

## Decision

**Matching is exact equality after Unicode NFC normalisation, case folding and
whitespace collapsing — never a substring, never a split.** The comparison key is
`normalize_tag`, the same function behind the archive's `tag_name_norm` columns, so "the
same genre" means one thing throughout the application rather than one thing per layer.

**Comma-separated input is accepted but never guessed at.** A line containing a comma is
reported with *both* readings — the whole value it would match and the fragments it would
become — and the caller decides. Splitting happens only when a caller explicitly asks
(`allow_commas=True`), which the UI offers as a separate, labelled action after showing
the consequence.

**Two questions are kept distinct in code**, because conflating them silently blacklists
nothing:

- `matches(value, entries)` — "is this literal string blacklisted?"
- `matches_any_piece(value, entries)` — "does any component of this multi-valued tag
  match?", splitting with the policy's own `SPLIT_PATTERN`. This is the write-path
  question, because a proposed genre is a piece.

**Removal from a packed value is a separate, explicit operation.**
`remove_component(value, target)` decomposes, drops matching components and rebuilds with
the separator the value already used — rewriting `Reggae; Ska` as `Reggae, Ska` would be
an unrelated restyle of something the operator did not ask to change. It returns
`replacement=None` when nothing survives rather than deciding whether that means "drop
the value" or "leave it"; the caller decides.

**Neither feature's matching is applied to curated data implicitly.** The blacklist
governs what may be *added*: `_merge` carries existing values through without consulting
it, so an item that already holds a blacklisted genre keeps it. Deleting a stored value
is the separate, reviewed, revertible removal operation.

## Consequences

- The destructive reading of ambiguous input is always an explicit choice, and the
  operator sees which spellings disappear before anything is written.
- A typo in a removal target selects nothing and reports "0 matched" rather than deleting
  a genre that merely resembles the intended one.
- Packed-value selection needs a scan, because Jellyfin cannot filter by *part* of a
  value. Bounded by `MAX_SCAN_ITEMS` and reported, in the same spirit as the browse
  `missing` filter. The exact-match path needs no scan at all, which is why it is the
  default.
- Blacklisting a genre does not clean up existing data, and the two features were
  deliberately not merged. A blacklist entry that retroactively deleted library genres on
  save would be a large, unattended write triggered by a settings change.
- Two call sites for "does this match?" is a real cost, paid because a single function
  answering both questions would have to guess which one was meant — and the guess that
  fails is the one that blacklists nothing, or deletes 67 albums.

## Alternatives rejected

- **Split input on commas, matching the requested format literally.** Silently converts
  one intended entry into two, and does it to `Rock, Reggae` — a genre that exists.
- **Substring or case-insensitive `contains` matching.** Removes `Gothic Rock`,
  `indie rock` and `rockabilly` when the operator blacklists `rock`.
- **Match the stored string without decomposing, for both features.** Then blacklisting
  `rock` does nothing at all to a tag supplied as `"Rock, Reggae"`, because the policy
  classified it into pieces before the blacklist saw it. The operator's entry appears
  saved and has no effect.
- **Decompose packed values by default.** Treats the common case (one genre whose name
  contains a separator) as the rare one, and edits values nobody asked to change.
- **Store the operator's input verbatim without normalising the uniqueness key.** `Rock`
  and `rock` would occupy two rows, the second would silently do nothing, and the operator
  would have no signal — the same argument ADR 0008's tag edges make about
  `(entity, tag_name_norm)`.
