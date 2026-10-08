/**
 * Facet filters and exclusion patterns: the parts that are decisions rather than markup.
 *
 * Kept out of the components for the same reason `paging.ts` is: every mistake here is
 * invisible in a rendered page. A dropped id silently *widens* a selection on the server
 * (Jellyfin discards an id list it cannot parse and answers with the whole library), and a
 * pattern that was trimmed at the wrong moment quietly stops matching. So the rules live in
 * plain functions that can be checked directly.
 */

import type { SelectionKind } from '../api/client.ts'

/**
 * The id shapes Jellyfin will parse.
 *
 * Anything else -- a typo, a bare word, a 40-character hex string, two ids joined by `|`
 * -- is *discarded in full*, and when nothing in the list parses the parameter is ignored
 * and the server answers with the unfiltered library. Verified live on 12.2.0:
 * `ArtistIds=abc` returns all 502 albums where `ArtistIds=<32 hex>` returns 2.
 *
 * So a bad id in the URL is dropped locally rather than sent. The URL is a link someone may
 * have edited, not a contract -- the same reason an unknown `sort` falls back instead of
 * 422-ing the screen.
 */
export const ITEM_ID_PATTERN =
  /^(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$/

export function isItemId(value: string): boolean {
  return ITEM_ID_PATTERN.test(value.trim())
}

/** Ids safe to send: shape-checked, trimmed, de-duplicated, order preserved. */
export function validIds(values: readonly string[]): string[] {
  const found: string[] = []
  for (const raw of values) {
    const candidate = raw.trim()
    if (!candidate || !isItemId(candidate)) continue
    if (!found.includes(candidate)) found.push(candidate)
  }
  return found
}

/**
 * Patterns as the server will match them: trimmed, blanks dropped, de-duplicated.
 *
 * Folded for comparison only -- the *stored* value keeps the operator's spelling, because
 * a chip that showed `*live*` after they typed `*LIVE*` would look like the input was
 * mangled, and the pattern is echoed back in the scan report.
 *
 * Deliberately no escaping and no implicit wildcards: `live` matches `live` and nothing
 * else. Adding `*` around it to be helpful is how a short word comes to swallow a library.
 */
export function cleanPatterns(values: readonly string[]): string[] {
  const found: string[] = []
  const seen = new Set<string>()
  for (const raw of values) {
    const candidate = raw.trim()
    if (!candidate) continue
    const key = candidate.toLocaleLowerCase()
    if (seen.has(key)) continue
    seen.add(key)
    found.push(candidate)
  }
  return found
}

/** Add a value unless an equivalent one is already there. */
export function addValue(
  current: readonly string[],
  value: string,
  { fold = false }: { fold?: boolean } = {},
): string[] {
  const candidate = value.trim()
  if (!candidate) return [...current]
  const key = fold ? candidate.toLocaleLowerCase() : candidate
  const exists = current.some((entry) =>
    fold ? entry.toLocaleLowerCase() === key : entry === key,
  )
  return exists ? [...current] : [...current, candidate]
}

export function removeValue(
  current: readonly string[],
  value: string,
  { fold = false }: { fold?: boolean } = {},
): string[] {
  const key = fold ? value.toLocaleLowerCase() : value
  return current.filter((entry) => (fold ? entry.toLocaleLowerCase() : entry) !== key)
}

/**
 * Which pickers a media type offers.
 *
 * Mirrors what the server refuses rather than merely what is useful: `artist_ids` does not
 * narrow an artist query and `album_ids` does not narrow an artist or an album one, and the
 * API answers those with a 422 because Jellyfin answers them with *zero* -- an empty table
 * that reads as a library with nothing in it.
 */
export function facetsFor(kind: SelectionKind): { artistIds: boolean; albumIds: boolean } {
  return {
    artistIds: kind === 'album' || kind === 'song',
    albumIds: kind === 'song',
  }
}

/**
 * A label for a chip or a result, disambiguated when two entities share a name.
 *
 * Not hypothetical: the live library has three artist names that map to two catalog
 * entities each (`Christian Death`, `The Crüxshadows`, `Various Artists`). Selecting by
 * name alone would make one twin unfilterable and the other a coin flip, so the ids are
 * what travel -- and the short id suffix is what lets an operator tell them apart.
 */
export function entityLabel(
  entry: { id: string; name: string },
  all: readonly { id: string; name: string }[],
): string {
  const duplicates = all.filter((other) => other.name === entry.name).length > 1
  return duplicates ? `${entry.name} · ${entry.id.slice(-6)}` : entry.name
}
