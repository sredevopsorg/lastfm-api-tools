import { useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { api, type ItemSummary, type LibraryInfo, type SelectionKind } from '../api/client'
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

export function Library() {
  const [params, setParams] = useSearchParams()
  const kind = (params.get('kind') as SelectionKind | null) ?? 'artist'
  const search = params.get('search') ?? ''
  const missingOnly = params.get('missing') === '1'
  const [draft, setDraft] = useState(search)

  const libraries = useQuery({
    queryKey: ['libraries'],
    queryFn: () => api.get<LibraryInfo[]>('/api/libraries'),
  })

  const items = useQuery({
    queryKey: ['items', kind, search],
    queryFn: () =>
      api.items({ kind, page_size: 200, ...(search ? { search } : {}) }),
  })

  function update(next: Record<string, string | null>) {
    const merged = new URLSearchParams(params)
    for (const [key, value] of Object.entries(next)) {
      if (value === null || value === '') merged.delete(key)
      else merged.set(key, value)
    }
    setParams(merged)
  }

  const visible = (items.data?.items ?? []).filter(
    (item) => !missingOnly || gaps(item).length > 0,
  )

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
        <table className="grid">
          <thead>
            <tr>
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
                <tr key={item.id}>
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
                <td colSpan={5} className="muted">
                  Nothing to show. Try clearing the filter, or fetching Last.fm data for
                  a specific item from its editor.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      )}
    </section>
  )
}
