/**
 * The paging and sorting arithmetic.
 *
 * Run with `node --test --experimental-strip-types src/components/paging.test.ts`, or via
 * `pnpm test`. No test runner is added for this: node 24 strips types natively, and every
 * mistake in this module is arithmetic, which needs assertions rather than a DOM.
 *
 * The cases below are the ones that are invisible in a rendered page. An off-by-one
 * window shows a row twice or not at all, and "page 3 of 2" is only ever noticed by
 * someone who happens to look at the number -- which is exactly the class of defect this
 * whole feature exists to remove, so they are asserted rather than eyeballed.
 */

import assert from 'node:assert/strict'
import { test } from 'node:test'

import {
  ariaSortFor,
  nextSort,
  pageCount,
  rangeLabel,
  windowFor,
} from './paging.ts'

test('a full first page reports the rows it returned', () => {
  const window = windowFor({ startIndex: 0, returned: 50 })
  assert.deepEqual(window, { start: 0, end: 50, firstRow: 1, lastRow: 50 })
  assert.equal(rangeLabel(window, 5442), '1–50 of 5,442')
})

test('a partial last page does not claim rows that do not exist', () => {
  // 5442 items, page size 50: the last page holds 42. Computing the range from the
  // *requested* size would say "5401–5450 of 5442", which is two rows past the end.
  const window = windowFor({ startIndex: 5400, returned: 42 })
  assert.deepEqual(window, { start: 5400, end: 5442, firstRow: 5401, lastRow: 5442 })
  assert.equal(rangeLabel(window, 5442), '5401–5442 of 5,442')
})

test('an empty result reports zero rather than one to zero', () => {
  const window = windowFor({ startIndex: 0, returned: 0 })
  assert.deepEqual(window, { start: 0, end: 0, firstRow: 0, lastRow: 0 })
  assert.equal(rangeLabel(window, 0), '0 of 0')
})

test('a start index past the end reports no window at all', () => {
  // Reachable by a stale link, or a filter that shrank the result set under a page the
  // user was already on. Every field must agree that there is nothing here: an earlier
  // version clamped `start` into range while leaving the row numbers at zero, so the
  // window claimed a position and simultaneously claimed to be empty.
  const window = windowFor({ startIndex: 9999, returned: 0 })
  assert.deepEqual(window, { start: 0, end: 0, firstRow: 0, lastRow: 0 })
  assert.equal(rangeLabel(window, 10), '0 of 10')
})

test('a negative start index is clamped rather than producing row zero', () => {
  // A hand-edited URL is the realistic path here.
  const window = windowFor({ startIndex: -50, returned: 10 })
  assert.deepEqual(window, { start: 0, end: 10, firstRow: 1, lastRow: 10 })
})

test('paging lands exactly on the item after the last one returned', () => {
  // The off-by-one that duplicates a row: advancing by `pageSize` instead of by what was
  // returned re-shows rows on a partial page.
  const first = windowFor({ startIndex: 0, returned: 50 })
  assert.equal(first.end, 50, 'next page starts where this one ended')
})

test('page count rounds up so a partial last page is a page', () => {
  assert.equal(pageCount(5442, 50), 109)
  assert.equal(pageCount(5400, 50), 108)
  assert.equal(pageCount(1, 50), 1)
  assert.equal(pageCount(0, 50), 1, 'an empty list is still one page, not zero')
})

test('page count never divides by zero', () => {
  assert.equal(pageCount(10, 0), 1)
  assert.equal(pageCount(10, -5), 1)
})

test('clicking a new column sorts it ascending', () => {
  const state = nextSort({ sort: 'name', order: 'desc' }, 'year')
  assert.deepEqual(state, { sort: 'year', order: 'asc' })
})

test('clicking the sorted column reverses it', () => {
  const ascending = nextSort({ sort: 'name', order: 'asc' }, 'name')
  assert.deepEqual(ascending, { sort: 'name', order: 'desc' })
  const descending = nextSort(ascending, 'name')
  assert.deepEqual(descending, { sort: 'name', order: 'asc' })
})

test('aria-sort names the direction and nothing else', () => {
  // A screen reader announces this; getting it wrong is a wrong statement, not a
  // cosmetic issue. `none` on an unsorted column is required, not optional.
  assert.equal(ariaSortFor({ sort: 'name', order: 'asc' }, 'name'), 'ascending')
  assert.equal(ariaSortFor({ sort: 'name', order: 'desc' }, 'name'), 'descending')
  assert.equal(ariaSortFor({ sort: 'name', order: 'asc' }, 'year'), 'none')
})
