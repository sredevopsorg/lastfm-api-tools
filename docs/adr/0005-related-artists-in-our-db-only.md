# 0005. Related artists live in our DB only, never written to Jellyfin

- **Status:** Accepted
- **Date:** 2026-10-06

## Context

`artist.getSimilar` returns a ranked, scored list of similar artists. Jellyfin's
`BaseItemDto` has no related-artist field on `MusicArtist`. The writable fields
that could superficially carry the data are `Tags` (free-form strings),
`ExternalUrls`, and `People` with `PersonKind.Artist`.

## Decision

Store similar artists in `lastfm_similarity`, in our own database, with the match
score and rank. Do not write them to Jellyfin in any form.

## Consequences

- Zero pollution of the tag namespace and zero risk of corrupting the
  cast/credit list (`People` is also used by Jellyfin's credit-completion logic).
- Full fidelity: name, MBID, match score and rank are all preserved, none of
  which a tag string could express.
- The data is visible in this tool's related-artists panel and archive explorer,
  and queryable offline, but not in Jellyfin clients. This is a stated
  limitation, not an oversight.
- If a future Jellyfin version exposes a structured similar-artists field, the
  data is already there to populate it.

## Alternatives rejected

- **Tag prefix such as `similar-artist:Radiohead`.** Rejected by the operator.
  It would survive in Jellyfin and be queryable via `GET /Items?tags=`, at the
  cost of tag-namespace pollution.
- **Bidirectional `People` entries.** Rejected: it overloads a field that means
  "credited person" and can mislead credit completion.
- **`ExternalUrls` only.** Rejected: it records a link, not a relationship, and
  cannot express ordering or similarity.
