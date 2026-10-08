/**
 * The query the browse screens build.
 *
 * This exists because of a bug this file would have caught: the library page once sent
 * `limit` to an endpoint that takes `page_size`, and every page silently came back at the
 * default size. The parameter *names* are now typed from the OpenAPI document, so a typo
 * is a compile error -- but the *values* are not, and the `missing` filter is the case
 * that matters: FastAPI takes a list as a repeated parameter, and sending it as one
 * comma-joined value is a 422 from a filter that looks applied.
 */

import assert from 'node:assert/strict'
import { test } from 'node:test'

import { queryString } from './client.ts'

test('a list becomes repeated parameters, not one comma-joined value', () => {
  // The bug: `missing=genres,tags` is a 422. `missing=genres&missing=tags` is the filter.
  const query = queryString({ missing: ['genres', 'tags'] })
  assert.equal(query, '?missing=genres&missing=tags')
})

test('a single-element list is still repeated syntax', () => {
  assert.equal(queryString({ missing: ['genres'] }), '?missing=genres')
})

test('an empty list contributes nothing rather than an empty parameter', () => {
  // `?missing=` would be an empty string, which FastAPI rejects for a list of literals.
  assert.equal(queryString({ missing: [] }), '')
})

test('falsy-but-meaningful values are kept', () => {
  // `start_index: 0` and `page: 1` are real values. Dropping them silently is how
  // "page 1" becomes "whatever the server defaults to".
  assert.equal(queryString({ start_index: 0 }), '?start_index=0')
  assert.equal(queryString({ page: 1 }), '?page=1')
})

test('empty strings and nulls are omitted so the server default applies', () => {
  assert.equal(queryString({ search: '', parent_id: null }), '')
})

test('the browse query for missing genres over songs is what the API expects', () => {
  const query = queryString({
    kind: 'song',
    page_size: 50,
    start_index: 100,
    sort: 'sort_name',
    order: 'asc',
    missing: ['genres'],
  })
  assert.equal(
    query,
    '?kind=song&page_size=50&start_index=100&sort=sort_name&order=asc&missing=genres',
  )
})
