/**
 * Facet and exclusion-pattern rules.
 *
 * Run with `node --test --experimental-strip-types src/components/facet_rules.test.ts`, or
 * via `pnpm test`.
 *
 * The cases below are the ones that are invisible in a rendered page. A dropped id does not
 * look wrong on screen -- it makes the list *longer*, because Jellyfin discards an id list
 * it cannot parse and answers with the whole library. A pattern trimmed at the wrong moment
 * quietly stops matching. Neither is something a person notices by looking.
 */

import assert from 'node:assert/strict'
import { test } from 'node:test'

import {
  addValue,
  cleanPatterns,
  entityLabel,
  facetsFor,
  isItemId,
  removeValue,
  validIds,
} from './facet_rules.ts'

const HEX = 'bbc3e56260455521dfda7effa56e14f2'
const HEX_2 = '55032a1fe7e44ec8b3723943cf7db850'
const DASHED = 'bbc3e562-6045-5521-dfda-7effa56e14f2'

test('the id shapes Jellyfin parses are accepted', () => {
  // Each was verified to work as a filter value on a live 12.2.0 server, including the
  // dashed and uppercase forms.
  for (const value of [HEX, HEX.toUpperCase(), DASHED, DASHED.toUpperCase(), ` ${HEX} `]) {
    assert.equal(isItemId(value), true, value)
  }
})

test('the id shapes Jellyfin discards are rejected', () => {
  // `<32 hex>|<32 hex>`, a 40-character string and a bare word each returned the *whole*
  // library when probed live.
  for (const value of ['', 'abc', '0'.repeat(40), `${HEX}|${HEX_2}`, `${HEX},${HEX_2}`, 'nope']) {
    assert.equal(isItemId(value), false, value)
  }
})

test('ids from the URL are filtered, trimmed and de-duplicated', () => {
  assert.deepEqual(validIds([` ${HEX} `, HEX, HEX_2, 'abc', '', HEX_2]), [HEX, HEX_2])
})

test('a bad id is dropped rather than sent', () => {
  // The direction matters. Sending `abc` would make the server ignore the whole parameter
  // and answer with the unfiltered library, so the screen would claim to filter while
  // showing everything -- and this screen's selection feeds a write.
  assert.deepEqual(validIds(['abc']), [])
  assert.deepEqual(validIds([`${HEX}|x`]), [])
})

test('order is preserved, so a link round-trips predictably', () => {
  assert.deepEqual(validIds([HEX_2, HEX]), [HEX_2, HEX])
})

test('patterns are trimmed, blanks dropped and case-folded for comparison', () => {
  assert.deepEqual(cleanPatterns(['  *Live* ', '*live*', '', '   ', 'Various Artists']), [
    '*Live*',
    'Various Artists',
  ])
})

test('a pattern keeps the spelling it was typed with', () => {
  // Only *comparison* folds case. Echoing `*live*` back for a typed `*LIVE*` would look
  // like the input was mangled, and the pattern is echoed again in the scan report.
  assert.deepEqual(cleanPatterns(['*LIVE*']), ['*LIVE*'])
})

test('adding a value is idempotent, and folds only when asked', () => {
  assert.deepEqual(addValue([HEX], HEX), [HEX])
  assert.deepEqual(addValue(['*Live*'], '*live*', { fold: true }), ['*Live*'])
  assert.deepEqual(addValue(['*Live*'], '*live*'), ['*Live*', '*live*'])
  assert.deepEqual(addValue([], '   '), [])
})

test('removing a value honours the same folding', () => {
  assert.deepEqual(removeValue(['*Live*'], '*live*', { fold: true }), [])
  assert.deepEqual(removeValue(['*Live*'], '*live*'), ['*Live*'])
  assert.deepEqual(removeValue([HEX, HEX_2], HEX), [HEX_2])
})

test('the facets offered match what the server will accept', () => {
  // Mirrors the API's refusal rather than merely what is useful: Jellyfin *applies*
  // `ArtistIds` to an artist query and returns zero (0 of 639, measured), so offering it
  // would show an empty table that reads as a library with nothing in it.
  assert.deepEqual(facetsFor('artist'), { artistIds: false, albumIds: false })
  assert.deepEqual(facetsFor('album'), { artistIds: true, albumIds: false })
  assert.deepEqual(facetsFor('song'), { artistIds: true, albumIds: true })
})

test('a repeated name is disambiguated by its id', () => {
  // Not hypothetical: the live library has three artist names that map to two catalog
  // entities each, so a name-only list would make one twin unpickable.
  const twins: { id: string; name: string }[] = [
    { id: 'da70314f257bfe5c3f55acfbf744e94b', name: 'Christian Death' },
    { id: '1b870621f48e594cda15e61e043b2b43', name: 'Christian Death' },
    { id: 'aaaabbbbccccddddeeeeffff00001111', name: 'Sole Artist' },
  ]
  // `noUncheckedIndexedAccess` is on, so destructuring gives `T | undefined` -- asserted
  // rather than non-null-asserted, because the point of the flag is that an index may not
  // exist and a `!` here would be the habit that defeats it everywhere else.
  const [first, second, sole] = twins
  assert.ok(first && second && sole)

  assert.equal(entityLabel(first, twins), 'Christian Death · 44e94b')
  assert.notEqual(entityLabel(first, twins), entityLabel(second, twins))
  assert.equal(entityLabel(sole, twins), 'Sole Artist')
})
