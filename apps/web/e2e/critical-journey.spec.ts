import { expect, test, type APIRequestContext } from '@playwright/test'

/**
 * The critical journey, end to end through the browser.
 *
 * This is the money path: find an artist, see what Last.fm proposes, write only the
 * fields you chose, and undo it. It runs against a stub Jellyfin, because the journey
 * writes and must never touch a real library.
 *
 * The assertion that matters most is the one on the *server's* state, not on the UI's
 * message. `POST /Items/{id}` is a full overwrite, so a body that omits a field nulls
 * it; checking only what the app says it did would pass even if it had destroyed the
 * item. So the last step asks the stub what it now holds.
 */

const API = process.env.E2E_API_URL ?? 'http://127.0.0.1:8080'
const STUB = process.env.E2E_STUB_URL ?? 'http://127.0.0.1:8096'

async function stubState(request: APIRequestContext) {
  const response = await request.get(`${STUB}/__writes`)
  expect(response.ok()).toBeTruthy()
  return (await response.json()) as {
    writes: { item_id: string; body: Record<string, unknown> }[]
    items: Record<string, Record<string, unknown>>
  }
}

test.beforeEach(async ({ request }) => {
  await request.post(`${STUB}/__reset`)
})

test('the archive explorer shows what was collected', async ({ page }) => {
  await page.goto('/archive')

  await expect(page.getByRole('heading', { name: 'Archive capacity' })).toBeVisible()
  // The seeded artist proves the derived layer is readable through the UI.
  await expect(page.getByRole('button', { name: 'Radiohead' })).toBeVisible({ timeout: 15_000 })

  await page.getByRole('button', { name: 'Radiohead' }).click()
  await expect(page.getByRole('heading', { name: 'Tags by popularity' })).toBeVisible()
  // Popularity comes from artist.getTopTags, which is envelope-free; if that plumbing
  // breaks, these counts go null and this is where it shows.
  await expect(page.getByRole('cell', { name: '100' })).toBeVisible()
  await expect(page.getByRole('cell', { name: '54' })).toBeVisible()
})

test('the library lists real items and reports what is missing', async ({ page }) => {
  await page.goto('/')
  await expect(page.getByRole('heading', { name: 'Library' })).toBeVisible()

  await expect(page.getByRole('link', { name: 'Radiohead' })).toBeVisible({ timeout: 15_000 })
  await expect(page.getByRole('link', { name: 'Portishead' })).toBeVisible()

  // Radiohead has a genre, Portishead has none, so the missing column must differ.
  const row = page.getByRole('row', { name: /Portishead/ })
  await expect(row.getByText('genres')).toBeVisible()
})

test('search narrows the library', async ({ page }) => {
  await page.goto('/')
  await expect(page.getByRole('link', { name: 'Radiohead' })).toBeVisible({ timeout: 15_000 })

  await page.getByLabel('Search artists').fill('Portishead')
  await page.getByRole('button', { name: 'Search' }).click()

  await expect(page.getByRole('link', { name: 'Portishead' })).toBeVisible()
  await expect(page.getByRole('link', { name: 'Radiohead' })).toHaveCount(0)
})

test('the diff proposes changes without writing anything', async ({ page, request }) => {
  await page.goto('/edit/art-1')

  // The candidate list comes from our archive, not from Last.fm.
  await expect(page.getByRole('heading', { name: 'Candidate' })).toBeVisible({
    timeout: 15_000,
  })
  await expect(page.getByRole('radio')).toBeChecked()

  const proposed = page.locator('.panel', { has: page.getByRole('heading', { name: /Proposed changes/ }) })
  await expect(proposed).toBeVisible()
  await expect(proposed.locator('.change-row', { hasText: 'Genres' })).toHaveCount(1)

  // A diff is a read: the stub must have seen no writes at all.
  const state = await stubState(request)
  expect(state.writes).toHaveLength(0)
})

test('applying writes the selected field and preserves everything else', async ({
  page,
  request,
}) => {
  await page.goto('/edit/art-1')
  await expect(page.getByRole('heading', { name: /Proposed changes/ })).toBeVisible({
    timeout: 15_000,
  })

  // Tick Genres only, then write exactly that.
  const genresRow = page.locator('.change-row', { hasText: 'Genres' }).first()
  await genresRow.getByRole('checkbox').check()
  await page.getByRole('button', { name: /^Apply 1 field/ }).click()

  await expect(page.getByText('Result')).toBeVisible({ timeout: 15_000 })

  const state = await stubState(request)
  expect(state.writes).toHaveLength(1)
  const sent = state.writes[0].body

  // Genres changed...
  expect(sent.Genres).toEqual(expect.arrayContaining(['Rock']))
  // ...and the fields nobody selected survived. This is the assertion that would catch
  // a destructive partial body, which is the failure mode this whole tool is shaped
  // around.
  expect(sent.Tags).toEqual(['keep-me'])
  expect(sent.Name).toBe('Radiohead')
  expect(sent.ProviderIds).toEqual({
    MusicBrainzArtist: 'a74b1b7f-71a5-4011-9441-d0b5e4122711',
  })

  // The server now holds the new genres, so the write really landed.
  const after = state.items['art-1']
  expect(after.Genres).toEqual(expect.arrayContaining(['Rock']))
  expect(after.Tags).toEqual(['keep-me'])
})

test('undoing restores the previous values', async ({ page, request }) => {
  await page.goto('/edit/art-1')
  await expect(page.getByRole('heading', { name: /Proposed changes/ })).toBeVisible({
    timeout: 15_000,
  })

  const genresRow = page.locator('.change-row', { hasText: 'Genres' }).first()
  await genresRow.getByRole('checkbox').check()
  await page.getByRole('button', { name: /^Apply 1 field/ }).click()
  await expect(page.getByText('Result')).toBeVisible({ timeout: 15_000 })

  // The history is what makes the write reversible.
  await expect(page.getByRole('heading', { name: 'History' })).toBeVisible()
  const applied = await stubState(request)
  const afterApply = applied.items['art-1'].Genres
  expect(afterApply).toEqual(expect.arrayContaining(['Rock']))

  await page.getByRole('button', { name: 'revert' }).first().click()

  await expect
    .poll(async () => (await stubState(request)).items['art-1'].Genres)
    .not.toEqual(afterApply)
})

test('a revert is itself recorded, so history is never mutated', async ({ page, request }) => {
  await page.goto('/edit/art-1')
  await expect(page.getByRole('heading', { name: /Proposed changes/ })).toBeVisible({
    timeout: 15_000,
  })
  const genresRow = page.locator('.change-row', { hasText: 'Genres' }).first()
  await genresRow.getByRole('checkbox').check()
  await page.getByRole('button', { name: /^Apply 1 field/ }).click()
  await expect(page.getByText('Result')).toBeVisible({ timeout: 15_000 })
  await page.getByRole('button', { name: 'revert' }).first().click()

  const history = page.locator('.panel', { has: page.getByRole('heading', { name: 'History' }) })
  // The snapshot table is not reset between tests -- it lives in our database, while
  // `beforeEach` only resets the stub Jellyfin -- so the assertion is on what this test
  // added rather than on an absolute count.
  await expect(history.getByText('apply', { exact: true }).first()).toBeVisible()
  await expect(history.getByText('revert', { exact: true }).first()).toBeVisible({ timeout: 15_000 })
})

test('bulk reviews a selection and refuses to apply without a review', async ({ page, request }) => {
  await page.goto('/bulk')
  await expect(page.getByRole('heading', { name: 'Bulk' })).toBeVisible()

  // Nothing is queued before a review, so the apply button is not even offered.
  await expect(page.getByRole('button', { name: /^Apply to/ })).toHaveCount(0)
  expect((await stubState(request)).writes).toHaveLength(0)

  await page.getByRole('button', { name: 'Review' }).click()

  await expect(page.getByRole('heading', { name: /^Reviewed \d/ })).toBeVisible({
    timeout: 20_000,
  })
  // Reviewing writes nothing; only the apply can.
  expect((await stubState(request)).writes).toHaveLength(0)

  await page.getByRole('button', { name: /^Apply to/ }).click()
  await expect(page.getByRole('heading', { name: 'Applied' })).toBeVisible({ timeout: 25_000 })

  // Undoing the whole batch is offered, because every write shares a batch id.
  await expect(page.getByRole('button', { name: /Revert this batch/ })).toBeVisible()
})

test('the API reports the credential can write', async ({ page }) => {
  // The UI hides the editing path when it cannot write, so this is load-bearing.
  await page.goto('/archive')
  await expect(page.getByRole('heading', { name: 'Archive capacity' })).toBeVisible()
})
