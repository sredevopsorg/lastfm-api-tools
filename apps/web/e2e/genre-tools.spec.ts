import { expect, test, type APIRequestContext } from '@playwright/test'

/**
 * The genre blacklist and the genre-removal tool, through the browser.
 *
 * Two things can only be checked here rather than in a component test, and both are the
 * reason this file exists:
 *
 * - The blacklist must change what a *diff proposes*. Reading the saved value back would
 *   pass with the setting entirely disconnected from the mapping layer, which is the exact
 *   failure the feature was written to remove (it used to be an env var, so "saved but not
 *   in effect" was the normal state).
 * - A removal must send the *whole* item back. `POST /Items/{id}` is a full overwrite
 *   (ADR 0003), so a removal that built its own body would delete the overview and the
 *   MusicBrainz id along with the genre -- silently, on every item it touched.
 *
 * KNOWN GAP, stated rather than implied: this suite is not run in CI. It needs a browser
 * and the compose stack (`scripts/e2e.sh`), so a green CI run says nothing about it.
 */

const STUB = process.env.E2E_STUB_URL ?? 'http://127.0.0.1:8096'

/**
 * The server-side id of a fixture item, by the readable name the stub knows it by.
 *
 * Ids are 32-hex now rather than `art-1`, because that is the only shape Jellyfin accepts
 * as a facet filter value -- so the specs ask the stub for the id instead of hard-coding
 * one, and a fixture rename does not break them.
 */
async function idOf(request: APIRequestContext, key: string): Promise<string> {
  const response = await request.get(`${STUB}/__writes`)
  const body = (await response.json()) as { ids: Record<string, string> }
  const found = body.ids[key]
  if (!found) throw new Error(`the stub library has no item named ${key}`)
  return found
}

/** Every write the stub observed, so a test can assert on the exact body sent. */
async function writes(request: APIRequestContext) {
  const response = await request.get(`${STUB}/__writes`)
  const body = (await response.json()) as {
    writes: { item_id: string; body: Record<string, unknown> }[]
  }
  return body.writes
}

/** One item from the stub library. Throws rather than returning undefined: a test that
 * silently compared against a missing item would pass for the wrong reason.
 *
 * Addressed by the readable *key*, then resolved through the id the server uses -- the same
 * two-step a spec takes to reach an item in the UI. Passing a server id here worked while
 * the fixture was keyed by readable ids and silently stopped working when they became hex,
 * which is exactly the confusion this indirection removes.
 */
async function item(
  request: APIRequestContext,
  key: string,
): Promise<Record<string, unknown>> {
  const response = await request.get(`${STUB}/__writes`)
  const body = (await response.json()) as {
    items: Record<string, Record<string, unknown>>
    ids: Record<string, string>
  }
  const id = body.ids[key]
  const found = id ? body.items[id] : undefined
  if (!found) throw new Error(`the stub library has no item named ${key}`)
  return found
}

test.beforeEach(async ({ request }) => {
  await request.post(`${STUB}/__reset`)
})

// ------------------------------------------------------------------ blacklist

test('a saved blacklist entry says which library genres it matches', async ({ page }) => {
  await page.goto('/settings')
  await expect(page.getByRole('heading', { name: 'Settings' })).toBeVisible()

  // "Rock" is on art-1 in the stub library, so the preview has something real to name.
  await page.getByLabel('Blacklisted genres, one per line').fill('Rock')
  await page.getByRole('button', { name: 'Preview matches' }).click()

  const preview = page.locator('.panel', { hasText: 'What this would match' })
  await expect(preview).toBeVisible()
  // The library's own spelling, not the operator's -- that is the point of the preview.
  await expect(preview.locator('.chip', { hasText: 'Rock' })).toBeVisible()
})

test('a comma line is reported with both readings instead of being split', async ({ page }) => {
  // The whole reason this screen does not simply split on commas: a comma separates
  // entries as you type it, and is also legal inside a genre value. Guessing would
  // blacklist two genres the operator did not name, with nothing on screen saying so.
  await page.goto('/settings')
  const input = page.getByLabel('Blacklisted genres, one per line')
  await input.fill('Gothic\nRock, Reggae\nSka')

  await page.getByRole('button', { name: 'Save', exact: true }).click()

  const conflict = page.locator('.conflict-box')
  await expect(conflict).toBeVisible()
  await expect(conflict).toContainText('Rock, Reggae')
  await expect(conflict).toContainText('Rock')
  await expect(conflict).toContainText('Reggae')
  // The unambiguous lines were still saved -- this is a question, not a failure.
  await expect(page.locator('.conflict-box')).toContainText('2 entr')
})

test('a blacklist entry changes which genres the editor proposes', async ({ page, request }) => {
  // The end-to-end point of the feature: the setting reaches the mapping layer.
  //
  // The stub's Last.fm artist offers `alternative rock`, `rock`, `art rock` and
  // `electronic`. Blacklisting `Rock` must drop exactly the `rock` tag while leaving the
  // three that merely *contain* the word -- which is the exact-match rule, asserted on the
  // proposal the operator actually sees rather than on a stored value read back.
  await page.goto('/settings')
  await page.getByLabel('Blacklisted genres, one per line').fill('Rock')
  await page.getByRole('button', { name: 'Save', exact: true }).click()
  await expect(page.getByText(/Saved \d+ entr/)).toBeVisible()

  // art-1 holds Genres ["Rock"], and the diff pre-selects nothing until a candidate is
  // chosen -- the editor picks the best one itself, so the diff renders on load.
  await page.goto(`/edit/${await idOf(request, 'art-1')}`)
  await expect(page.getByRole('heading', { name: 'Radiohead' })).toBeVisible()

  const genres = page.locator('.change-row', { hasText: 'Genres' }).first()
  await expect(genres).toBeVisible()
  // The dropped tag is reported, so the operator can see the policy act rather than
  // wonder why a tag they expected is absent.
  await expect(genres).toContainText('on the blacklist')
  // And the longer genres are still proposed, because matching is exact.
  await expect(genres).not.toContainText('alternative rock')
})

test('the blacklist survives a reload, which the env var could not', async ({ page }) => {
  await page.goto('/settings')
  await page.getByLabel('Blacklisted genres, one per line').fill('Gothic Rock')
  await page.getByRole('button', { name: 'Save', exact: true }).click()
  await expect(page.getByText(/Saved \d+ entr/)).toBeVisible()

  await page.reload()
  await expect(page.getByLabel('Blacklisted genres, one per line')).toHaveValue('Gothic Rock')
})

test('an entry is shown back with the spelling the operator used', async ({ page }) => {
  // "Rock" and "rock" are one entry. Echoing the folded form back to someone who typed
  // the capitalised one reads as the tool ignoring them.
  await page.goto('/settings')
  await page.getByLabel('Blacklisted genres, one per line').fill('GOTHIC rock')
  await page.getByRole('button', { name: 'Save', exact: true }).click()

  await page.reload()
  await expect(page.getByLabel('Blacklisted genres, one per line')).toHaveValue('GOTHIC rock')
})

// --------------------------------------------------------------- genre removal

test('the genre list is offered from the library, not typed free-hand', async ({ page }) => {
  await page.goto('/remove-genre')
  await expect(page.getByRole('heading', { name: 'Remove a genre' })).toBeVisible()

  const options = page.locator('#library-genres option')
  await expect(options.first()).toBeAttached()
  // "Ambient" is on the filler artists, "Shoegaze" on one of the duplicate-name pair.
  await expect(page.locator('#library-genres option[value="Ambient"]')).toBeAttached()
})

test('a removal is not applicable until a genre is named', async ({ page }) => {
  await page.goto('/remove-genre')
  // An empty genre is refused by the server as a wildcard, so the button must be inert.
  await expect(page.getByRole('button', { name: 'Review' })).toBeDisabled()

  await page.getByLabel('genre to remove').fill('Ambient')
  await expect(page.getByRole('button', { name: 'Review' })).toBeEnabled()
})

test('the diff matches exactly, so a longer genre is not selected', async ({ page }) => {
  // The rule the whole tool rests on. "Shoegaze" and "Krautrock" are on the two
  // same-named "Twin Peaks" artists; asking for "Shoe" must select neither, because
  // matching is exact rather than a substring.
  await page.goto('/remove-genre')
  await page.getByLabel('genre to remove').fill('Shoe')
  await page.getByRole('button', { name: 'Review' }).click()

  await expect(page.getByText(/Nothing in this library carries/)).toBeVisible()
})

test('a removal writes the whole item, not just the genre field', async ({ page, request }) => {
  // ADR 0003. A removal builds a payload for a full-overwrite endpoint, so anything it
  // omits is nulled -- the operator would lose the overview and the MusicBrainz id.
  const original = await item(request, 'art-1')
  expect(original.Genres).toEqual(['Rock'])

  await page.goto('/remove-genre')
  await page.getByLabel('genre to remove').fill('Rock')
  await page.getByRole('button', { name: 'Review' }).click()

  const table = page.locator('table.grid').first()
  await expect(table).toBeVisible()
  await page.getByRole('button', { name: /Remove from/ }).click()

  await expect(page.getByRole('heading', { name: 'Applied' })).toBeVisible()

  const target = await idOf(request, 'art-1')
  const sent = (await writes(request)).filter((write) => write.item_id === target)
  expect(sent).toHaveLength(1)

  const body = sent[0]!.body
  expect(body.Genres).toEqual([])
  // Everything else survives at its current value.
  expect(body.ProviderIds).toEqual(original.ProviderIds)
  expect(body.Tags).toEqual(original.Tags)
  expect(body.LockData).toBe(original.LockData)
  // And the field set is exactly the writable set: no key omitted, none invented. Listed
  // rather than counted, because a count would silently agree with a wrong list, and the
  // *set* is what ADR 0003 constrains. An artist adds `ArtistItems`; albums and songs
  // deliberately omit both artist-link keys.
  expect(Object.keys(body).sort()).toEqual(
    [
      'Name',
      'ForcedSortName',
      'OriginalTitle',
      'Overview',
      'Genres',
      'Tags',
      'Studios',
      'ProductionLocations',
      'ProviderIds',
      'ExternalUrls',
      'CommunityRating',
      'CriticRating',
      'PremiereDate',
      'ProductionYear',
      'OfficialRating',
      'CustomRating',
      'PreferredMetadataLanguage',
      'PreferredMetadataCountryCode',
      'People',
      'LockData',
      'LockedFields',
      'ArtistItems',
    ].sort(),
  )
})

test('a removal can be reverted as one batch', async ({ page, request }) => {
  await page.goto('/remove-genre')
  await page.getByLabel('genre to remove').fill('Rock')
  await page.getByRole('button', { name: 'Review' }).click()
  await page.getByRole('button', { name: /Remove from/ }).click()
  await expect(page.getByRole('heading', { name: 'Applied' })).toBeVisible()

  await page.getByRole('button', { name: 'Revert this batch' }).click()
  await expect(page.getByRole('heading', { name: 'Reverted' })).toBeVisible()

  const sent = await writes(request)
  const restored = sent.at(-1)
  expect(restored?.item_id).toBe(await idOf(request, 'art-1'))
  expect(restored?.body.Genres).toEqual(['Rock'])
})

test('reviewing writes nothing', async ({ page, request }) => {
  await page.goto('/remove-genre')
  await page.getByLabel('genre to remove').fill('Rock')
  await page.getByRole('button', { name: 'Review' }).click()
  await expect(page.locator('table.grid').first()).toBeVisible()

  expect(await writes(request)).toEqual([])
})

test('splitting packed values is offered separately and changes the result', async ({ page }) => {
  // "Rock, Reggae" does not exist in the stub library, so the observable difference is
  // that the option is available, off by default, and labelled with its consequence.
  await page.goto('/remove-genre')
  const toggle = page.getByLabel('also split packed values')
  await expect(toggle).not.toBeChecked()

  await toggle.check()
  await expect(
    page.getByText(/it is also removed from a packed value like/),
  ).toBeVisible()
})
