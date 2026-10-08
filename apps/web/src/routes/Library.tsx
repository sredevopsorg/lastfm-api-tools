import { useEffect, useRef, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import {
  api,
  type HarvestEvent,
  type ItemsQuery,
  type HarvestItemEvent,
  type ItemSummary,
  type LibraryInfo,
  type SelectionKind,
} from '../api/client'
import { ErrorNote } from '../components/ui'
import { FilterChips, Pagination, ScanNote, SortHeader } from '../components/table'
import type { SortState } from '../components/paging'

const KINDS: { value: SelectionKind; label: string }[] = [
  { value: 'artist', label: 'Artists' },
  { value: 'album', label: 'Albums' },
  { value: 'song', label: 'Songs' },
]

/** What an item is missing, as the columns an operator scans for. */
function gaps(item: ItemSummary) {
  const missing: string[] = []
  if (item.genres.length === 0) missing.push('genres')
  if (item.tags.length === 0) missing.push('tags')
  if (!item.has_overview) missing.push('overview')
  if (!item.has_provider_ids) missing.push('ids')
  return missing
}

/**
 * The aspects the server will filter on, per media type.
 *
 * A song with no overview is normal rather than a gap -- measured on the live library,
 * 5,441 of 5,442 songs have none -- so offering "missing overview" for songs would
 * produce a filter that selects almost everything. The backend drops inapplicable aspects
 * silently; the UI must not offer them in the first place, or it teaches an operator that
 * the filter is broken.
 */
const ASPECTS_BY_KIND: Record<SelectionKind, { value: string; label: string }[]> = {
  artist: [
    { value: 'genres', label: 'genres' },
    { value: 'tags', label: 'tags' },
    { value: 'overview', label: 'overview' },
    { value: 'provider_ids', label: 'ids' },
  ],
  album: [
    { value: 'genres', label: 'genres' },
    { value: 'tags', label: 'tags' },
    { value: 'overview', label: 'overview' },
    { value: 'provider_ids', label: 'ids' },
  ],
  song: [
    { value: 'genres', label: 'genres' },
    { value: 'tags', label: 'tags' },
    { value: 'provider_ids', label: 'ids' },
  ],
}

/** Sort keys the server accepts, per media type. `year` is meaningless for an artist. */
const SORTS_BY_KIND: Record<SelectionKind, { value: string; label: string }[]> = {
  artist: [
    { value: 'sort_name', label: 'Name' },
    { value: 'name', label: 'Display name' },
    { value: 'date_added', label: 'Added' },
  ],
  album: [
    { value: 'sort_name', label: 'Name' },
    { value: 'name', label: 'Display name' },
    { value: 'year', label: 'Year' },
    { value: 'date_added', label: 'Added' },
  ],
  song: [
    { value: 'sort_name', label: 'Name' },
    { value: 'name', label: 'Display name' },
    { value: 'date_added', label: 'Added' },
  ],
}

const PAGE_SIZE = 50

/** A media type from the URL, or the default. An unknown one must not 422 the screen. */
function readKind(value: string | null): SelectionKind {
  return KINDS.some((entry) => entry.value === value) ? (value as SelectionKind) : 'artist'
}

interface HarvestState {
  done: number
  total: number
  // Only `item` frames are kept: the summary and reindex frames are about the batch,
  // not about an item, and carrying the whole union would make every reader narrow it.
  events: HarvestItemEvent[]
}

/**
 * Browse and select library items, then fetch their Last.fm data.
 *
 * Every part of the query -- kind, search, sort, order, page, filters -- lives in the URL
 * rather than in component state, so a refresh, a back button and a shared link all mean
 * the same thing. Selection does not, and deliberately: it is a working set that survives
 * paging (see `selectionKey` below) but should not be spelled out in a URL that someone
 * might open expecting a plain view.
 */
export function Library() {
  const [params, setParams] = useSearchParams()
  const navigate = useNavigate()
  const queryClient = useQueryClient()

  const kind = readKind(params.get('kind'))
  const search = params.get('search') ?? ''
  // Validated against what this media type accepts rather than trusted: every one of these
  // arrives from a hand-editable URL, and an unrecognised `sort` is a 422 from the API
  // rather than a fallback. Falling back locally is the friendly behaviour and the honest
  // one -- the URL is a link someone may have edited, not a contract.
  const availableSorts = SORTS_BY_KIND[kind]
  const requestedSort = params.get('sort') ?? ''
  const sort = availableSorts.some((entry) => entry.value === requestedSort)
    ? requestedSort
    : 'sort_name'
  const order: 'asc' | 'desc' = params.get('order') === 'desc' ? 'desc' : 'asc'
  const startIndex = Math.max(0, Number(params.get('start') ?? 0) || 0)
  const aspects = params
    .getAll('missing')
    .filter((aspect) => ASPECTS_BY_KIND[kind].some((entry) => entry.value === aspect))
  const [draft, setDraft] = useState(search)

  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [harvesting, setHarvesting] = useState<HarvestState | null>(null)
  const [harvestError, setHarvestError] = useState<unknown>(null)
  const abort = useRef<AbortController | null>(null)

  const sortState: SortState = { sort, order }

  const libraries = useQuery({
    queryKey: ['libraries'],
    queryFn: () => api.get<LibraryInfo[]>('/api/libraries'),
  })

  const items = useQuery({
    queryKey: ['items', kind, search, sort, order, startIndex, aspects.join(',')],
    queryFn: () =>
      api.items({
        kind,
        page_size: PAGE_SIZE,
        start_index: startIndex,
        sort: sort as NonNullable<ItemsQuery['sort']>,
        order,
        ...(search ? { search } : {}),
        ...(aspects.length ? { missing: aspects as NonNullable<ItemsQuery['missing']> } : {}),
      }),
  })

  const rows = items.data?.items ?? []
  const scan = items.data?.scan ?? null

  // Selection is cleared when the *query* changes and kept when only the page does.
  // Selecting across pages is the reason paging exists; carrying a selection into a
  // different search result would attach writes to items the operator never saw.
  const selectionKey = [kind, search, sort, order, aspects.join(',')].join('|')
  const lastSelectionKey = useRef(selectionKey)
  useEffect(() => {
    if (lastSelectionKey.current !== selectionKey) {
      lastSelectionKey.current = selectionKey
      setSelected(new Set())
      setHarvesting(null)
    }
  }, [selectionKey])

  function update(next: Record<string, string | null | undefined>) {
    const merged = new URLSearchParams(params)
    for (const [key, value] of Object.entries(next)) {
      if (value === null || value === undefined || value === '') merged.delete(key)
      else merged.set(key, value)
    }
    // Any change to the query resets to the first page. Staying on page 7 of a result set
    // that just shrank is how a table appears empty for no visible reason.
    if (!('start' in next)) merged.delete('start')
    setParams(merged)
  }

  function applySort(next: SortState) {
    update({ sort: next.sort, order: next.order })
  }

  function toggleAspect(aspect: string) {
    const next = aspects.includes(aspect)
      ? aspects.filter((entry) => entry !== aspect)
      : [...aspects, aspect]
    const merged = new URLSearchParams(params)
    merged.delete('missing')
    merged.delete('start')
    for (const entry of next) merged.append('missing', entry)
    setParams(merged)
  }

  function toggle(itemId: string) {
    setSelected((current) => {
      const next = new Set(current)
      if (next.has(itemId)) next.delete(itemId)
      else next.add(itemId)
      return next
    })
  }

  const pageSelected = rows.filter((item) => selected.has(item.id)).length
  const allPageSelected = rows.length > 0 && pageSelected === rows.length

  function togglePage() {
    setSelected((current) => {
      const next = new Set(current)
      if (allPageSelected) for (const item of rows) next.delete(item.id)
      else for (const item of rows) next.add(item.id)
      return next
    })
  }

  /** Fetch Last.fm data for the selection, streaming progress per item. */
  async function runHarvest() {
    const ids = [...selected]
    if (ids.length === 0) return
    setHarvestError(null)
    setHarvesting({ done: 0, total: ids.length, events: [] })
    abort.current = new AbortController()
    const collected: HarvestItemEvent[] = []
    try {
      await api.stream<HarvestEvent>(
        '/api/harvest',
        { selection: { kind, ids }, search_fallback: true },
        (event) => {
          if (event.type === 'item') {
            collected.push(event)
            setHarvesting({ done: collected.length, total: ids.length, events: [...collected] })
          } else if (event.type === 'error') {
            setHarvestError(new Error(event.message))
          }
        },
        abort.current.signal,
      )
    } catch (caught) {
      setHarvestError(caught)
    } finally {
      // The archive changed, so anything derived from it is stale.
      void queryClient.invalidateQueries({ queryKey: ['archive-entities'] })
      void queryClient.invalidateQueries({ queryKey: ['archive-stats'] })
      void queryClient.invalidateQueries({ queryKey: ['candidates'] })
    }
  }

  const events = harvesting?.events ?? []
  const found = events.filter((event) => event.found).length
  const matchedIds = events.filter((event) => event.found).map((event) => event.item_id)
  const availableAspects = ASPECTS_BY_KIND[kind]

  const activeFilters = [
    ...(search ? [{ key: 'search', label: `search: ${search}` }] : []),
    ...aspects.map((aspect) => ({ key: `missing:${aspect}`, label: `missing ${aspect}` })),
  ]

  return (
    <section>
      <div className="row">
        <h2>Library</h2>
        <div className="pill-row">
          {KINDS.map((entry) => (
            <button
              key={entry.value}
              className={entry.value === kind ? 'pill active' : 'pill'}
              onClick={() => update({ kind: entry.value, missing: null, sort: null })}
            >
              {entry.label}
            </button>
          ))}
        </div>
      </div>

      {libraries.data && (
        <p className="muted small">
          {libraries.data.length === 0
            ? 'No music library found on this server.'
            : `Music libraries: ${libraries.data.map((l) => l.name).join(', ')}`}
          {items.data ? ` · ${items.data.total.toLocaleString()} ${kind}s match` : ''}
        </p>
      )}

      <div className="row">
        <form
          className="search"
          onSubmit={(event) => {
            event.preventDefault()
            update({ search: draft })
          }}
        >
          <input
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            placeholder={`Search ${kind}s…`}
            aria-label={`Search ${kind}s`}
          />
          <button type="submit">Search</button>
          {search && (
            <button
              type="button"
              onClick={() => {
                setDraft('')
                update({ search: null })
              }}
            >
              Clear
            </button>
          )}
        </form>

        <label className="check">
          sort
          <select
            value={sort}
            onChange={(event) => update({ sort: event.target.value })}
            aria-label="Sort by"
          >
            {availableSorts.map((entry) => (
              <option key={entry.value} value={entry.value}>
                {entry.label}
              </option>
            ))}
          </select>
        </label>
        <label className="check">
          <input
            type="checkbox"
            checked={order === 'desc'}
            onChange={(event) => update({ order: event.target.checked ? 'desc' : null })}
          />
          descending
        </label>
      </div>

      {/* Aspects are checkboxes rather than a free-text filter: the set is small, closed,
          and differs per media type, so a list is both easier to use and impossible to
          misspell into a filter that silently matches nothing. */}
      <div className="row">
        <span className="muted small">missing:</span>
        {availableAspects.map((aspect) => (
          <label className="check" key={aspect.value}>
            <input
              type="checkbox"
              checked={aspects.includes(aspect.value)}
              onChange={() => toggleAspect(aspect.value)}
            />
            {aspect.label}
          </label>
        ))}
      </div>

      <FilterChips
        filters={activeFilters}
        onRemove={(key) => {
          if (key === 'search') {
            setDraft('')
            update({ search: null })
          } else {
            toggleAspect(key.slice('missing:'.length))
          }
        }}
        onClear={() => {
          setDraft('')
          const merged = new URLSearchParams(params)
          merged.delete('search')
          merged.delete('missing')
          merged.delete('start')
          setParams(merged)
        }}
      />

      {items.isPending && <p className="muted">Loading…</p>}
      {items.isError && <ErrorNote error={items.error} />}
      {libraries.isError && <ErrorNote error={libraries.error} />}

      {items.data && (
        <>
          {scan && (
            <ScanNote
              scanned={scan.scanned}
              matched={scan.matched}
              truncated={scan.truncated}
              limit={scan.limit}
            />
          )}

          <div className="panel actions selection-bar">
            <span>
              <strong>{selected.size}</strong> selected
              {pageSelected > 0 && selected.size !== pageSelected ? (
                <span className="muted small"> ({pageSelected} on this page)</span>
              ) : null}
            </span>
            <button className="pill" onClick={togglePage} disabled={rows.length === 0}>
              {allPageSelected ? `Deselect these ${rows.length}` : `Select these ${rows.length}`}
            </button>
            {selected.size > 0 && (
              <button className="pill" onClick={() => setSelected(new Set())}>
                Clear
              </button>
            )}
            <button
              className="primary"
              onClick={runHarvest}
              disabled={selected.size === 0 || harvesting !== null}
            >
              {harvesting
                ? `Fetching ${harvesting.done}/${harvesting.total}…`
                : `Fetch Last.fm data for ${selected.size}`}
            </button>
            {found > 0 && (
              <button
                className="primary"
                onClick={() => navigate(`/review?kind=${kind}&ids=${matchedIds.join(',')}`)}
              >
                Review {found} matched →
              </button>
            )}
            <span className="muted small">
              Fetching fills the local archive. It reads Last.fm and writes nothing to
              Jellyfin.
            </span>
          </div>

          {harvestError != null && <ErrorNote error={harvestError} />}

          {events.length > 0 && (
            <div className="panel">
              <h3>
                Fetched{' '}
                <span className="muted small">
                  {found} matched · {events.length - found} not found
                </span>
              </h3>
              <table className="grid">
                <thead>
                  <tr>
                    <th>Item</th>
                    <th>Looked up as</th>
                    <th>Last.fm methods</th>
                    <th>Outcome</th>
                  </tr>
                </thead>
                <tbody>
                  {events.map((event) => (
                    <tr key={event.item_id}>
                      <td>{event.name}</td>
                      <td className="mono small">
                        {Object.entries(event.derived_query)
                          .filter(([, value]) => value)
                          .map(([key, value]) => `${key}=${value}`)
                          .join(' · ')}
                      </td>
                      <td className="chips">
                        {event.methods.map((method) => (
                          <span className="chip mono" key={method}>
                            {method}
                          </span>
                        ))}
                      </td>
                      <td>
                        {event.found ? (
                          <span className="badge ok">found</span>
                        ) : (
                          <>
                            <span className="badge warn">not found</span>
                            {event.alternatives.length > 0 ? (
                              <span className="muted small">
                                {' '}
                                close matches:{' '}
                                {event.alternatives.slice(0, 3).map((a) => a.name).join(', ')}
                              </span>
                            ) : (
                              <span className="muted small"> {event.error}</span>
                            )}
                          </>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <div className="table-wrap">
            <table className="grid">
              <thead>
                <tr>
                  <th style={{ width: '2.5rem' }}>
                    <input
                      type="checkbox"
                      checked={allPageSelected}
                      onChange={togglePage}
                      aria-label="Select every row on this page"
                    />
                  </th>
                  <SortHeader
                    column="sort_name"
                    label="Name"
                    current={sortState}
                    onSort={applySort}
                  />
                  <SortHeader
                    column="year"
                    label="Year"
                    current={sortState}
                    onSort={applySort}
                    width="5rem"
                  />
                  <th>Genres</th>
                  <th>Tags</th>
                  <th>Missing</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {rows.map((item) => {
                  const missing = gaps(item)
                  return (
                    <tr key={item.id} className={selected.has(item.id) ? 'row-selected' : ''}>
                      <td>
                        <input
                          type="checkbox"
                          checked={selected.has(item.id)}
                          onChange={() => toggle(item.id)}
                          aria-label={`Select ${item.name}`}
                        />
                      </td>
                      <td>
                        <Link to={`/edit/${item.id}?kind=${item.kind}`}>{item.name}</Link>
                        {item.album_artist && (
                          <span className="muted small"> — {item.album_artist}</span>
                        )}
                      </td>
                      <td className="muted small">{item.year ?? '—'}</td>
                      <td className="chips">
                        {item.genres.length === 0 ? (
                          <span className="muted">—</span>
                        ) : (
                          item.genres.slice(0, 3).map((genre) => (
                            <span className="chip" key={genre}>
                              {genre}
                            </span>
                          ))
                        )}
                      </td>
                      <td className="chips">
                        {item.tags.length === 0 ? (
                          <span className="muted">—</span>
                        ) : (
                          item.tags.slice(0, 3).map((tag) => (
                            <span className="chip" key={tag}>
                              {tag}
                            </span>
                          ))
                        )}
                      </td>
                      <td>
                        {missing.length === 0 ? (
                          <span className="badge ok">complete</span>
                        ) : (
                          <span className="badge warn">{missing.join(', ')}</span>
                        )}
                      </td>
                      <td>
                        <Link to={`/edit/${item.id}?kind=${item.kind}`}>open</Link>
                      </td>
                    </tr>
                  )
                })}
                {rows.length === 0 && (
                  <tr>
                    <td colSpan={7} className="muted">
                      {activeFilters.length > 0
                        ? 'No items match these filters. Remove one to widen the search.'
                        : 'Nothing to show. Try clearing the filter, or search for a title.'}
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>

          <Pagination
            startIndex={startIndex}
            pageSize={PAGE_SIZE}
            returned={rows.length}
            total={items.data.total}
            onStartIndex={(next) => update({ start: next === 0 ? null : String(next) })}
          />
        </>
      )}
    </section>
  )
}
