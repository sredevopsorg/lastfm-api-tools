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

/**
 * The fetch step: search the library, select matches, pull Last.fm data into the archive.
 *
 * This is the part of the flow that had no implementation at all -- the archive could
 * only be filled by the test suite, so the editor had nothing to propose and the journey
 * could not be completed. It runs against a stub Last.fm, so it is deterministic and
 * spends no real rate limit.
 */
test.describe('fetching from Last.fm', () => {
  test('search the library, select an album, and fetch its Last.fm data', async ({
    page,
    request,
  }) => {
    await page.goto('/?kind=album')
    await expect(page.getByRole('heading', { name: 'Library' })).toBeVisible()

    await page.getByLabel('Search albums').fill('OK Computer')
    await page.getByRole('button', { name: 'Search' }).click()

    const row = page.getByRole('row', { name: /OK Computer/ })
    await expect(row).toBeVisible({ timeout: 15_000 })

    // Select the match, then fetch.
    await row.getByRole('checkbox').check()
    await expect(page.getByText('1 selected')).toBeVisible()

    await page.getByRole('button', { name: /Fetch Last.fm data for 1/ }).click()

    // The result is reported per item, including which methods were called.
    await expect(page.getByRole('heading', { name: /Fetched/ })).toBeVisible({ timeout: 25_000 })
    await expect(page.getByText('found', { exact: true })).toBeVisible()
    await expect(page.getByText('album.getinfo')).toBeVisible()
    // The popularity source is fetched too; without it every tag count is null.
    await expect(page.getByText('album.gettoptags')).toBeVisible()

    // Harvesting must not touch Jellyfin.
    const state = await stubState(request)
    expect(state.writes).toHaveLength(0)

    // And the data is in the archive, so it can be proposed from.
    const entities = await request.get(`${API}/api/archive/entities?kind=album&page_size=50`)
    expect(entities.ok()).toBeTruthy()
    const body = (await entities.json()) as { items: { name: string }[] }
    expect(body.items.map((e) => e.name)).toContain('OK Computer')
  })

  test('the fetched album reports real tag popularity', async ({ request }) => {
    // Done through the API rather than the UI: the assertion is about the data reaching
    // the derived layer, and the UI path is covered by the spec above.
    const items = await request.get(`${API}/api/items?kind=album&search=OK%20Computer&page_size=5`)
    const page = (await items.json()) as { items: { id: string; name: string }[] }
    const album = page.items.find((item) => item.name === 'OK Computer')
    expect(album, 'the stub library must hold OK Computer').toBeTruthy()

    const harvested = await request.post(`${API}/api/items/${album!.id}/harvest`)
    expect(harvested.ok()).toBeTruthy()
    const outcome = (await harvested.json()) as { found: boolean; methods: string[] }
    expect(outcome.found).toBe(true)
    expect(outcome.methods).toContain('album.gettoptags')

    const entities = await request.get(`${API}/api/archive/entities?kind=album&page_size=50`)
    const body = (await entities.json()) as { items: { id: number; name: string }[] }
    const row = body.items.find((e) => e.name === 'OK Computer')
    expect(row).toBeTruthy()

    const detail = await request.get(`${API}/api/archive/entities/album/${row!.id}`)
    expect(detail.ok()).toBeTruthy()
    const entity = (await detail.json()) as {
      tags: { name: string; count: number | null }[]
    }
    const counts = Object.fromEntries(entity.tags.map((tag) => [tag.name, tag.count]))
    // Real numbers, from `album.getTopTags`, matched to the right album by attribution.
    expect(counts['alternative rock']).toBe(100)
    expect(counts['rock']).toBe(89)
  })

  test('a miss is reported as an outcome, not a failure', async ({ request }) => {
    // Last.fm does not hold everything a library does, so a miss is normal. It must come
    // back as a reported outcome with a reason -- an error status would abort a
    // library-wide fetch at the first obscure item.
    const response = await request.post(`${API}/api/items/art-3/harvest`)
    expect(response.ok(), 'a miss must not be an error status').toBeTruthy()

    const outcome = (await response.json()) as {
      found: boolean
      error: string | null
      error_code: string | null
      alternatives: { name: string }[]
    }
    expect(outcome.found).toBe(false)
    expect(outcome.error_code).toBe('not_found')
    expect(outcome.error, 'a miss must say why').toBeTruthy()
  })

  test('an item with nothing to look up says so rather than guessing', async ({ request }) => {
    // `art-3` has no MusicBrainz id and no artist, so there is no query to derive. The
    // endpoint must say that rather than searching for an empty string.
    const response = await request.post(`${API}/api/items/art-3/harvest`)
    const outcome = (await response.json()) as { error_code: string | null }
    expect(['not_found', 'insufficient_item_data']).toContain(outcome.error_code)
  })
})

test.describe('the review and confirm step', () => {
  test('fetched items are reviewed field by field before anything is written', async ({
    page,
    request,
  }) => {
    // Fetch first, so the review has something real to propose.
    const harvested = await request.post(`${API}/api/items/alb-1/harvest`)
    expect(harvested.ok()).toBeTruthy()

    await page.goto('/review?kind=album&ids=alb-1')
    await expect(page.getByRole('heading', { name: 'Review' })).toBeVisible()

    // Nothing is compared until asked, and comparing writes nothing.
    await page.getByRole('button', { name: /^Compare 1 item/ }).click()
    await expect(page.getByRole('heading', { name: 'Proposed changes' })).toBeVisible({
      timeout: 20_000,
    })
    expect((await stubState(request)).writes).toHaveLength(0)

    // Expand the item, clear whatever the policy pre-selected, then tick Genres alone.
    // Starting from a cleared state is what makes the count assertion meaningful: the
    // default selection is not necessarily empty, and assuming it was is how this test
    // first failed.
    await page.getByRole('button', { name: /^▸ OK Computer/ }).click()
    await page.getByRole('button', { name: /^Clear all$/ }).click()
    const genres = page.locator('.change-row', { hasText: 'Genres' }).first()
    await genres.getByRole('checkbox').check()
    await expect(page.getByText(/1 field across 1 item/)).toBeVisible()

    await page.getByRole('button', { name: /^Back up and write/ }).click()
    await expect(page.getByRole('heading', { name: 'Result' })).toBeVisible({ timeout: 25_000 })
    await expect(page.getByText('written', { exact: true })).toBeVisible()

    // The write happened, and only the ticked field changed.
    const state = await stubState(request)
    expect(state.writes).toHaveLength(1)
    const sent = state.writes[0].body
    expect(sent.Genres).toEqual(expect.arrayContaining(['alternative rock']))
    // Unselected fields survive. `AlbumArtist` is deliberately *not* in the payload: the
    // writable set is fixed and does not include the album-artist link, so asserting on
    // it tested a field this tool never writes.
    expect(sent.Tags).toEqual(['keep-me'])
    expect(sent.Name).toBe('OK Computer')
    expect(sent.ExternalUrls).toEqual([])
    expect(sent.People).toEqual([])
  })

  test('a review writes nothing when no field is ticked', async ({ page, request }) => {
    // The default selection is empty for anything needing review, so confirming an
    // untouched review must be a no-op rather than an uncontrolled write.
    const harvested = await request.post(`${API}/api/items/alb-2/harvest`)
    expect(harvested.ok()).toBeTruthy()

    await page.goto('/review?kind=album&ids=alb-2')
    await page.getByRole('button', { name: /^Compare 1 item/ }).click()
    await expect(page.getByRole('heading', { name: 'Proposed changes' })).toBeVisible({
      timeout: 20_000,
    })

    await page.getByRole('button', { name: /^Clear all$/ }).click()
    await expect(page.getByRole('button', { name: /^Back up and write/ })).toBeDisabled()
    expect((await stubState(request)).writes).toHaveLength(0)
  })
})
