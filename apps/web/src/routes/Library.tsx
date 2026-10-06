import { useMemo, useRef, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import {
  api,
  type HarvestEvent,
  type HarvestItemEvent,
  type ItemSummary,
  type LibraryInfo,
  type SelectionKind,
} from '../api/client'
import { ErrorNote } from '../components/ui'

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
 * Selection lives in the URL rather than in a store, so it survives a refresh and can be
 * linked to. The fetch is the step that fills the archive: until it has run for an item
 * there is nothing on Last.fm's side to propose, and the editor would have no candidate.
 */
export function Library() {
  const [params, setParams] = useSearchParams()
  const navigate = useNavigate()
  const queryClient = useQueryClient()

  const kind = (params.get('kind') as SelectionKind | null) ?? 'artist'
  const search = params.get('search') ?? ''
  const missingOnly = params.get('missing') === '1'
  const [draft, setDraft] = useState(search)

  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [harvesting, setHarvesting] = useState<HarvestState | null>(null)
  const [harvestError, setHarvestError] = useState<unknown>(null)
  const abort = useRef<AbortController | null>(null)

  const libraries = useQuery({
    queryKey: ['libraries'],
    queryFn: () => api.get<LibraryInfo[]>('/api/libraries'),
  })

  const items = useQuery({
    queryKey: ['items', kind, search],
    queryFn: () => api.items({ kind, page_size: 200, ...(search ? { search } : {}) }),
  })

  const visible = useMemo(
    () => (items.data?.items ?? []).filter((item) => !missingOnly || gaps(item).length > 0),
    [items.data, missingOnly],
  )

  function update(next: Record<string, string | null>) {
    const merged = new URLSearchParams(params)
    for (const [key, value] of Object.entries(next)) {
      if (value === null || value === '') merged.delete(key)
      else merged.set(key, value)
    }
    setParams(merged)
    // A new query is a different set of items, so a stale selection would mislead.
    setSelected(new Set())
    setHarvesting(null)
  }

  function toggle(itemId: string) {
    setSelected((current) => {
      const next = new Set(current)
      if (next.has(itemId)) next.delete(itemId)
      else next.add(itemId)
      return next
    })
  }

  const allVisibleSelected =
    visible.length > 0 && visible.every((item) => selected.has(item.id))

  function toggleAll() {
    setSelected(allVisibleSelected ? new Set() : new Set(visible.map((item) => item.id)))
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

  return (
    <section>
      <div className="row">
        <h2>Library</h2>
        <div className="pill-row">
          {KINDS.map((entry) => (
            <button
              key={entry.value}
              className={entry.value === kind ? 'pill active' : 'pill'}
              onClick={() => update({ kind: entry.value })}
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
          {items.data ? ` · ${items.data.total} ${kind}s` : ''}
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
          <input
            type="checkbox"
            checked={missingOnly}
            onChange={(event) => update({ missing: event.target.checked ? '1' : null })}
          />
          only items missing metadata
        </label>
      </div>

      {items.isPending && <p className="muted">Loading…</p>}
      {items.isError && <ErrorNote error={items.error} />}
      {libraries.isError && <ErrorNote error={libraries.error} />}

      {items.data && (
        <>
          <div className="panel actions selection-bar">
            <span>
              <strong>{selected.size}</strong> selected
            </span>
            <button className="pill" onClick={toggleAll} disabled={visible.length === 0}>
              {allVisibleSelected ? 'Clear page' : `Select all ${visible.length}`}
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

          <table className="grid">
            <thead>
              <tr>
                <th style={{ width: '2.5rem' }}>
                  <input
                    type="checkbox"
                    checked={allVisibleSelected}
                    onChange={toggleAll}
                    aria-label="Select all rows"
                  />
                </th>
                <th>Name</th>
                <th>Genres</th>
                <th>Tags</th>
                <th>Missing</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {visible.map((item) => {
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
              {visible.length === 0 && (
                <tr>
                  <td colSpan={6} className="muted">
                    Nothing to show. Try clearing the filter, or search for a title.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </>
      )}
    </section>
  )
}
