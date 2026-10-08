import { expect, test, type APIRequestContext, type Page } from '@playwright/test'

/**
 * The facet filters and exclusion patterns, through the browser.
 *
 * These exist because every guarantee in this feature is invisible in a passing screenshot.
 * An id of the wrong shape does not *look* wrong -- Jellyfin discards an unparseable id list
 * in full and answers with the whole library, so the symptom of a broken filter is a
 * *longer* list. A pattern that fails to exclude leaves items in a selection that feeds a
 * write. So the assertions here are on counts the server computed and on rows that are
 * present or absent, never on the controls merely being rendered.
 *
 * KNOWN GAP, stated rather than implied: this suite is not run in CI. It needs a browser and
 * a compose stack (`scripts/e2e.sh`). A green CI run says nothing about these specs.
 */

const STUB = process.env.E2E_STUB_URL ?? 'http://127.0.0.1:8096'

/**
 * The server-side id of a fixture item, by the readable name the stub knows it by.
 *
 * Not hard-coded: ids are 32 hex characters because that is the only shape Jellyfin parses
 * as a filter value -- and the only shape the app will send. A fixture using `art-1` would
 * make every facet test pass against a client that sent nothing at all, and hard-coding hex
 * would make every spec brittle to a fixture rename.
 */
async function idOf(request: APIRequestContext, key: string): Promise<string> {
  const response = await request.get(`${STUB}/__writes`)
  const body = (await response.json()) as { ids: Record<string, string> }
  const found = body.ids[key]
  if (!found) throw new Error(`the stub library has no item named ${key}`)
  return found
}

test.beforeEach(async ({ request }) => {
  await request.post(`${STUB}/__reset`)
})

/**
 * Pick one entity in a facet picker: search for it, then click its match.
 *
 * Scoped to the picker's own result list rather than searching the whole page for a button
 * whose name starts with the term -- the library table also renders a link per row, and an
 * unscoped locator would match a table row and pass for the wrong reason.
 */
async function pick(page: Page, facet: string, term: string) {
  const picker = page.locator('.facet-picker', { has: page.getByLabel(`Find ${facet} to filter by`) })
  await picker.getByLabel(`Find ${facet} to filter by`).fill(term)
  const results = picker.getByRole('button', { name: new RegExp(`^${term}`) })
  await expect(results.first()).toBeVisible({ timeout: 15_000 })
  await results.first().click()
  // The picker is controlled by the URL, so the chip is what proves the round trip landed.
  await expect(picker.locator('.chip')).toHaveCount(1, { timeout: 15_000 })
}

test('albums can be narrowed to one artist', async ({ page }) => {
  await page.goto('/?kind=album')
  await expect(page.getByRole('heading', { name: 'Library' })).toBeVisible()
  await expect(page.getByText(/3 albums match/)).toBeVisible({ timeout: 15_000 })

  await pick(page, 'artists', 'Radiohead')

  // Two of the three fixture albums are Radiohead's: "OK Computer" and "Live at Leeds".
  // The count comes from the server, so this asserts the filter was applied there rather
  // than over one already-fetched page.
  await expect(page.getByText(/2 albums match/)).toBeVisible({ timeout: 15_000 })
  await expect(page.getByRole('link', { name: 'OK Computer' })).toBeVisible()
  await expect(page.getByRole('link', { name: 'Dummy' })).toHaveCount(0)
})

test('the artist filter is carried in the URL, so a link reproduces it', async ({
  page,
  request,
}) => {
  const RADIOHEAD = await idOf(request, 'art-1')
  // URL-owned state is what makes a filtered list shareable, and it is also the only way to
  // prove the value that travelled is the id rather than a name that happens to match.
  await page.goto(`/?kind=album&artist_ids=${RADIOHEAD}`)
  await expect(page.getByText(/2 albums match/)).toBeVisible({ timeout: 15_000 })

  await page.reload()
  await expect(page.getByText(/2 albums match/)).toBeVisible()
})

test('songs can be narrowed by artist and by album', async ({ page, request }) => {
  const RADIOHEAD = await idOf(request, 'art-1')
  // `AlbumIds` applies to songs only, and this is the media type where both facets are
  // available -- so it is the one place the two can be told apart.
  await page.goto(`/?kind=song&artist_ids=${RADIOHEAD}`)
  await expect(page.getByText(/2 songs match/)).toBeVisible({ timeout: 15_000 })
  await expect(page.getByRole('link', { name: 'Roads' })).toHaveCount(0)

  // `AlbumIds` is the facet that applies to songs and nothing else.
  await page.goto(`/?kind=song&album_ids=${await idOf(request, 'alb-2')}`)
  await expect(page.getByText(/1 songs match/)).toBeVisible({ timeout: 15_000 })
  await expect(page.getByRole('link', { name: 'Roads' })).toBeVisible()
})

test('an unusable id in the URL is dropped rather than sent', async ({ page }) => {
  // The whole point of the guard. `ArtistIds=abc` on a live 12.2.0 server returns the
  // *entire* library -- 502 albums where a valid id returns 2 -- because the parameter is
  // discarded in full when nothing parses. A screen that forwarded it would show everything
  // while still claiming to filter.
  await page.goto('/?kind=album&artist_ids=abc')

  await expect(page.getByText(/3 albums match/)).toBeVisible({ timeout: 15_000 })
  await expect(page.getByRole('button', { name: /Remove artists/ })).toHaveCount(0)
})

test('an exclusion pattern hides matching items and says how many', async ({ page }) => {
  await page.goto('/?kind=album')
  await expect(page.getByText(/3 albums match/)).toBeVisible({ timeout: 15_000 })

  await page.getByLabel('Exclusion pattern').first().fill('*Live*')
  await page.getByRole('button', { name: 'Add pattern' }).first().click()

  // "Live at Leeds" is the only match, and the scan note is what distinguishes a filter
  // that dropped one item from a filter that dropped everything.
  await expect(page.getByText(/2 albums match/)).toBeVisible({ timeout: 15_000 })
  await expect(page.getByRole('link', { name: 'Live at Leeds' })).toHaveCount(0)
  // The note counts what survived, not what was dropped: three albums checked, one hidden
  // by the pattern, two left. `excluded` is a separate number in the API response.
  await expect(page.locator('.scan-note')).toContainText('2 matched, from 3 checked')
})

test('a pattern matching the album artist excludes a whole album', async ({ page }) => {
  // The exclusion that matters most in a real library, and the one a name-only matcher
  // would miss: "Various Artists" appears in the album artist, not in any track or album
  // name. Here the fixture's artist string is the thing being matched.
  await page.goto('/?kind=song&exclude=Radiohead')

  await expect(page.getByText(/1 songs match/)).toBeVisible({ timeout: 15_000 })
  await expect(page.getByRole('link', { name: 'Roads' })).toBeVisible()
  await expect(page.getByRole('link', { name: 'Airbag' })).toHaveCount(0)
  await expect(page.getByText(/exclude Radiohead/)).toBeVisible()
})

test('a bare pattern is literal, so it does not swallow a longer name', async ({ page }) => {
  // `android` with no wildcards matches exactly `android`, which nothing is called. A
  // substring matcher would hide "Paranoid Android" here -- which is how a short word comes
  // to silently empty a list.
  await page.goto('/?kind=song&exclude=android')

  await expect(page.getByText(/3 songs match/)).toBeVisible({ timeout: 15_000 })
  await expect(page.getByRole('link', { name: 'Paranoid Android' })).toBeVisible()
})

test('a pattern that excludes everything says so instead of blaming the filters', async ({
  page,
}) => {
  // `*` is a legal pattern, so an empty table is the control working. Telling the operator
  // to "remove a filter to widen the search" would be an explanation that is simply wrong.
  await page.goto('/?kind=album&exclude=*')

  await expect(page.getByText(/Every item checked \(3\) matched an exclusion pattern/)).toBeVisible(
    { timeout: 15_000 },
  )
})

test('the selection is cleared when a facet changes, because it feeds a write', async ({
  page,
}) => {
  // Selecting across *pages* is the point of paging; carrying a selection into a different
  // result set is how a batch would write to items the operator filtered away.
  //
  // Asserted as a transition -- "1 selected" becomes "0 selected" -- rather than as the
  // absence of a string. The first version checked `toHaveCount(0)` on `'0 selected'`,
  // which is text that never renders while a selection exists, so it passed whatever the
  // screen did and would have passed with the clearing removed entirely. A test that cannot
  // fail is not verification, and this one was found by failing on a later run for an
  // unrelated-looking reason.
  await page.goto('/')
  await expect(page.getByRole('link', { name: 'Filler Artist 001' })).toBeVisible({
    timeout: 15_000,
  })
  await page.getByLabel('Select Filler Artist 001').check()
  await expect(page.getByText('1 selected')).toBeVisible()

  await page.getByLabel('Exclusion pattern').first().fill('*Filler*')
  await page.getByRole('button', { name: 'Add pattern' }).first().click()

  // The count itself has to change. `selectionKey` includes the patterns, so adding one
  // changes the query and the effect clears the selection.
  await expect(page.getByText('0 selected')).toBeVisible({ timeout: 15_000 })
  await expect(page.getByText('1 selected')).toHaveCount(0)
})

test('the album facet is not offered for an artist library, which the server would refuse', async ({
  page,
}) => {
  // Jellyfin *applies* `AlbumIds` to an artist query and returns zero -- an empty table that
  // reads as an empty library. The API answers that with a 422, so the control must not be
  // offered in the first place rather than offered and then failing.
  await page.goto('/?kind=artist')
  await expect(page.getByRole('heading', { name: 'Library' })).toBeVisible()

  await expect(page.getByLabel('Find albums to filter by')).toHaveCount(0)
  await expect(page.getByLabel('Find artists to filter by')).toHaveCount(0)
})

test('switching media type drops a facet that cannot apply to the new one', async ({
  page,
  request,
}) => {
  const RADIOHEAD = await idOf(request, 'art-1')
  // Carrying `artist_ids` from albums to artists would leave a parameter the API refuses,
  // and the list would 422 rather than fall back -- so the values go with the media type.
  await page.goto(`/?kind=album&artist_ids=${RADIOHEAD}`)
  await expect(page.getByText(/2 albums match/)).toBeVisible({ timeout: 15_000 })

  await page.getByRole('button', { name: 'Artists', exact: true }).click()
  await expect(page.getByText(/artists match/)).toBeVisible({ timeout: 15_000 })
  await expect(page).toHaveURL(/kind=artist/)
  expect(page.url()).not.toContain('artist_ids')
})

test('the song facet picker finds an artist by name and keeps the id', async ({
  page,
  request,
}) => {
  await page.goto('/?kind=song')
  await pick(page, 'artists', 'Portishead')

  await expect(page.getByText(/1 songs match/)).toBeVisible({ timeout: 15_000 })
  // The chip shows the *name*; the URL carries the *id*. The name is what makes it readable
  // and the id is what makes it unambiguous -- the live library has three artist names that
  // map to two catalog entities each.
  await expect(page.getByRole('button', { name: 'Remove artists Portishead' })).toBeVisible()
  expect(page.url()).toContain(await idOf(request, 'art-2'))
})
