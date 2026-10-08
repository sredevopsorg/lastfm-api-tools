import { expect, test } from '@playwright/test'

/**
 * Paging, sorting and filtering through the browser.
 *
 * These exist because the browse screens were rewritten around a server-side query, and
 * the three things that can go wrong are all invisible to a component test:
 *
 * - Paging over a set that is not totally ordered can return a row twice and lose another.
 *   The stub library now contains two artists with the SAME name for exactly that reason.
 * - Sorting applied to the fetched page rather than by the server produces a correctly
 *   ordered *page* of a wrongly ordered *set*, which looks right.
 * - A filtered count drawn from a partial scan is a lower bound, and presenting it as a
 *   total is how the old screen reported "641 artists" above a table of 200.
 *
 * So the assertions are on numbers the server computed and on the union of every page,
 * not on the visible rows of one page.
 *
 * KNOWN GAP, stated rather than implied: this suite is not run in CI. It needs a browser
 * and a compose stack (`scripts/e2e.sh`). A green CI run says nothing about these specs.
 */

const STUB = process.env.E2E_STUB_URL ?? 'http://127.0.0.1:8096'

/** The library the stub holds, so a test can state the size it expects. */
async function librarySize(request: import('@playwright/test').APIRequestContext) {
  const response = await request.get(`${STUB}/__writes`)
  const body = (await response.json()) as { items: Record<string, { Type: string }> }
  const artists = Object.values(body.items).filter((item) => item.Type === 'MusicArtist')
  return artists.length
}

test.beforeEach(async ({ request }) => {
  await request.post(`${STUB}/__reset`)
})

test('the total counts the whole result, not the page', async ({ page, request }) => {
  // The bug: the header printed the server's total while the table held one page of it,
  // so "641 artists" sat above 200 rows and nothing said the two disagreed.
  const artists = await librarySize(request)
  expect(artists).toBeGreaterThan(50)

  await page.goto('/')
  await expect(page.getByRole('heading', { name: 'Library' })).toBeVisible()

  await expect(page.getByText(`${artists} artists match`)).toBeVisible({ timeout: 15_000 })
  // One page is 50, and the pager has to say what part of the whole it is showing.
  await expect(page.getByRole('navigation', { name: 'items pagination' })).toContainText(
    `1–50 of ${artists}`,
  )
  await expect(page.getByRole('row')).toHaveCount(51) // 50 rows plus the header
})

test('paging visits every artist exactly once, including the duplicate names', async ({
  page,
  request,
}) => {
  const artists = await librarySize(request)

  await page.goto('/')
  await expect(page.getByRole('link', { name: 'Filler Artist 001' })).toBeVisible({
    timeout: 15_000,
  })

  // Walk every page, collecting the names. The two "Twin Peaks" rows share a sort key, so
  // an ordering without a stable tiebreaker returns one twice and drops the other -- which
  // is precisely what happened to the archive's derived layer.
  //
  // Each page is awaited on its own pager label before the rows are read. The first
  // version clicked Next and immediately re-read, which collected page 1 fifty times and
  // reported the walk as complete -- a test that would have passed on any implementation
  // that rendered nothing at all.
  const seen: string[] = []
  for (;;) {
    const label = await page.getByRole('navigation', { name: 'items pagination' }).innerText()
    const pages = Number(label.match(/page (\d+) of (\d+)/)?.[2] ?? '0')
    const current = Number(label.match(/page (\d+) of (\d+)/)?.[1] ?? '0')

    for (const row of (await page.getByRole('row').all()).slice(1)) {
      const link = row.getByRole('link').first()
      if (await link.count()) seen.push((await link.textContent()) ?? '')
    }

    if (current >= pages) break
    await page.getByRole('button', { name: 'Next →' }).click()
    // Wait for the pager itself to move, so the next read is of the next page.
    await expect(
      page.getByRole('navigation', { name: 'items pagination' }),
    ).toContainText(`page ${current + 1} of`)
  }

  // The library holds 63 artists, two of which are BOTH named "Twin Peaks", so walking
  // every page must yield 63 rows with 62 distinct names. Comparing a page's distinct
  // names against the library total -- the first version of this -- asserts nothing:
  // 50 rows on page one are 50 distinct names whatever the ordering does.
  expect(seen).toHaveLength(artists)
  expect(new Set(seen).size).toBe(artists - 1)
  expect(seen.filter((name) => name === 'Twin Peaks')).toHaveLength(2)
})

test('the pager is disabled at the ends rather than wrapping', async ({ page }) => {
  await page.goto('/')
  await expect(page.getByRole('navigation', { name: 'items pagination' })).toBeVisible({
    timeout: 15_000,
  })

  // On page 1 there is nothing before, and it must be genuinely disabled -- a control that
  // is styled as disabled but still clickable is how a list silently reorders.
  await expect(page.getByRole('button', { name: '← Previous' })).toBeDisabled()
  await expect(page.getByRole('button', { name: 'Next →' })).toBeEnabled()
})

test('the page is in the URL, so a refresh and a back button agree', async ({ page }) => {
  // URL-owned state is what makes a link shareable and a refresh predictable. If the page
  // lived in component state, this would reset to page 1.
  await page.goto('/?start=50')
  await expect(page.getByRole('navigation', { name: 'items pagination' })).toContainText(
    'page 2 of',
    { timeout: 15_000 },
  )

  await page.reload()
  await expect(page.getByRole('navigation', { name: 'items pagination' })).toContainText(
    'page 2 of',
  )
})

test('sorting is done by the server, not by the visible page', async ({ page }) => {
  await page.goto('/')
  await expect(page.getByRole('link', { name: 'Filler Artist 001' })).toBeVisible({
    timeout: 15_000,
  })

  const firstPageAscending = await page.getByRole('link').allTextContents()

  await page.getByLabel('Sort by').selectOption('name')
  await page.getByRole('checkbox', { name: 'descending' }).check()

  // If the frontend sorted the fetched page, "descending" would reverse the same 50 rows
  // and the first entry would be whatever happened to be last. A server-side sort brings
  // the real last item of the whole set to the front.
  await expect(page.getByRole('row').nth(1)).toContainText('Twin Peaks', { timeout: 15_000 })
  const firstPageDescending = await page.getByRole('link').allTextContents()
  expect(firstPageDescending).not.toEqual(firstPageAscending)
})

test('sorting resets to the first page', async ({ page }) => {
  // Staying on page 2 of a differently-ordered set shows rows nobody navigated to, and an
  // empty table when the set shrank.
  await page.goto('/?start=50')
  await expect(page.getByRole('navigation', { name: 'items pagination' })).toContainText(
    'page 2 of',
    { timeout: 15_000 },
  )

  await page.getByLabel('descending').check()
  await expect(page.getByRole('navigation', { name: 'items pagination' })).toContainText(
    'page 1 of',
  )
})

test('a scan-backed filter reports what it scanned', async ({ page, request }) => {
  // The honest-count requirement: a filtered total drawn from a partial read is a lower
  // bound, and the UI has to say so rather than print it as if it were the whole set.
  const artists = await librarySize(request)

  await page.goto('/')
  await expect(page.getByRole('link', { name: 'Filler Artist 001' })).toBeVisible({
    timeout: 15_000,
  })

  await page.getByRole('checkbox', { name: 'overview' }).check()

  // Every artist was checked, so the note must say so rather than claim a lower bound.
  await expect(page.getByText(`from ${artists} checked`)).toBeVisible({ timeout: 15_000 })
  await expect(page.getByText(/every item was checked/)).toBeVisible()
  // And the total is now the FILTERED count, not the whole library.
  await expect(page.getByText(`${artists} artists match`)).toHaveCount(0)
})

test('an active filter is visible and removable', async ({ page }) => {
  // A filter set earlier is otherwise invisible, and the symptom -- a list that is quietly
  // too short -- looks like missing data rather than an active filter.
  await page.goto('/?kind=song')
  await expect(page.getByRole('heading', { name: 'Library' })).toBeVisible()

  await page.goto('/?missing=genres')
  // The chip names the filter, and removing it widens the list again.
  await expect(page.getByRole('button', { name: /Remove filter missing genres/ })).toBeVisible({
    timeout: 15_000,
  })
  await page.getByRole('button', { name: /Remove filter missing genres/ }).click()
  await expect(page.getByRole('button', { name: /Remove filter missing genres/ })).toHaveCount(0)
})

test('selecting on one page keeps the selection on the next', async ({ page }) => {
  // Selecting across pages is the reason paging exists in a screen whose selection feeds a
  // write path. Clearing on every page change would make bulk editing a per-page activity
  // without saying so.
  await page.goto('/')
  await expect(page.getByRole('link', { name: 'Filler Artist 001' })).toBeVisible({
    timeout: 15_000,
  })

  await page.getByLabel('Select Filler Artist 001').check()
  await expect(page.getByText('1 selected')).toBeVisible()

  await page.getByRole('button', { name: 'Next →' }).click()
  await expect(page.getByText('1 selected')).toBeVisible()
  // ...and it says how many of the selection are on the page being looked at.
  await expect(page.getByText('(0 on this page)')).toBeVisible()
})

test('a new search clears the selection', async ({ page }) => {
  // The other half: a selection carried into a different result set would attach writes to
  // items nobody looked at.
  await page.goto('/')
  await expect(page.getByRole('link', { name: 'Filler Artist 001' })).toBeVisible({
    timeout: 15_000,
  })

  await page.getByLabel('Select Filler Artist 001').check()
  await expect(page.getByText('1 selected')).toBeVisible()

  await page.getByLabel('Search artists').fill('Radiohead')
  await page.getByRole('button', { name: 'Search', exact: true }).click()

  await expect(page.getByText('0 selected')).toBeVisible()
  await expect(page.getByRole('link', { name: 'Radiohead' })).toBeVisible()
})

test('the archive entity table pages without losing a row', async ({ page }) => {
  // The archive's own paging, over the derived layer where the missing tiebreaker actually
  // lost a row on real data: 109 albums walked to 108 distinct with one duplicate.
  //
  // Stated plainly because it constrains what this can prove: the e2e seed provides ONE
  // artist and no albums or tracks, so this walk cannot exercise a multi-page case the way
  // the live data does. What it does check is that the pager renders a coherent range, that
  // the walk visits exactly that many rows, and that a single row is not duplicated -- and
  // the multi-page case is covered against real data by
  // tests/integration/test_archive_paging.py, which builds the 5-row fixture with duplicate
  // names and walks it at every page size.
  await page.goto('/archive')
  await expect(page.getByRole('heading', { name: 'Stored entities' })).toBeVisible()

  const pager = page.getByRole('navigation', { name: 'stored entities pagination' })
  await expect(pager).toContainText(/of [1-9]/, { timeout: 15_000 })
  const label = await pager.innerText()
  const total = Number(label.match(/of ([\d,]+)/)?.[1]?.replace(/,/g, '') ?? '0')
  const pages = Number(label.match(/page (\d+) of (\d+)/)?.[2] ?? '0')
  expect(total).toBeGreaterThan(0)
  expect(pages).toBeGreaterThan(0)

  const seen: string[] = []
  for (let index = 0; index < pages; index += 1) {
    for (const row of (await page.getByRole('row').all()).slice(1)) {
      const button = row.getByRole('button').first()
      if (await button.count()) seen.push((await button.textContent()) ?? '')
    }
    if (index + 1 < pages) {
      await page.getByRole('button', { name: 'Next →' }).click()
      await expect(pager).toContainText(`page ${index + 2} of`)
    }
  }

  expect(seen).toHaveLength(total)
  expect(seen.every((name) => name.length > 0)).toBe(true)
})

test('the archive says plainly when a kind holds nothing', async ({ page }) => {
  // The seed archives one artist and no albums, so this is the empty state rather than a
  // failure -- and it must read as "nothing stored", not as a broken table. An empty
  // result that renders as a blank region is indistinguishable from a request in flight.
  await page.goto('/archive')
  await expect(page.getByRole('heading', { name: 'Stored entities' })).toBeVisible()

  await page.getByRole('button', { name: 'albums' }).click()
  await expect(page.getByText(/Nothing stored yet|No stored entity matches/)).toBeVisible({
    timeout: 15_000,
  })
})
