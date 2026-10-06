import { useMemo, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import {
  api,
  type BulkEvent,
  type DiffResponse,
  type SelectionKind,
} from '../api/client'
import { ChangeRow, ConfidenceBadge, ErrorNote, WithheldRow } from '../components/ui'

interface ReviewedItem {
  item_id: string
  name: string
  kind: string
  applicable: boolean
  skipped_reason: string | null
  diff: DiffResponse
}

/**
 * Review what will be written, then write it.
 *
 * This is the confirmation step of the flow, and it exists so that "apply" is never
 * reachable from "search": the user sees each item's proposed changes, ticks the fields
 * they want, and only then writes. Nothing here runs automatically.
 *
 * Ticks start from each item's *default selection*, which is empty for anything that
 * needs review -- so an item nobody looked at writes nothing rather than writing
 * whatever Last.fm happened to return.
 */
export function Review() {
  const [params] = useSearchParams()
  const queryClient = useQueryClient()
  const kind = (params.get('kind') as SelectionKind | null) ?? 'artist'
  const ids = useMemo(
    () => (params.get('ids') ?? '').split(',').filter(Boolean),
    [params],
  )

  const [reviewed, setReviewed] = useState<ReviewedItem[] | null>(null)
  const [jobId, setJobId] = useState<string | null>(null)
  const [batchId, setBatchId] = useState<string | null>(null)
  // Per item: which fields the user ticked. Keyed by item id so an item can be
  // expanded and adjusted without touching the others.
  const [chosen, setChosen] = useState<Record<string, Set<string>>>({})
  const [open, setOpen] = useState<Set<string>>(new Set())
  const [diffing, setDiffing] = useState(false)
  const [applying, setApplying] = useState(false)
  const [progress, setProgress] = useState<BulkEvent[]>([])
  const [error, setError] = useState<unknown>(null)
  const abort = useRef<AbortController | null>(null)

  const archiveReady = useQuery({
    queryKey: ['archive-stats'],
    queryFn: () => api.get<{ derived: { total: number } }>('/api/archive/stats'),
  })

  async function runDiff() {
    setError(null)
    setDiffing(true)
    setProgress([])
    abort.current = new AbortController()
    const collected: ReviewedItem[] = []
    try {
      await api.stream<BulkEvent>(
        '/api/bulk/diff',
        { selection: { kind, ids } },
        (event) => {
          if (event.type === 'item') {
            collected.push(event)
            setReviewed([...collected])
          } else if (event.type === 'summary') {
            setJobId(event.job_id ?? null)
            setBatchId(event.batch_id ?? null)
          } else if (event.type === 'error') {
            setError(new Error(event.message))
          }
        },
        abort.current.signal,
      )
      // Seed the ticks from each item's safe default, so the operator starts from the
      // changes the policy is already confident about.
      const initial: Record<string, Set<string>> = {}
      for (const item of collected) {
        initial[item.item_id] = new Set(item.diff.default_selection)
      }
      setChosen(initial)
    } catch (caught) {
      setError(caught)
    } finally {
      setDiffing(false)
    }
  }

  function toggleField(itemId: string, field: string) {
    setChosen((current) => {
      const next = new Set(current[itemId] ?? [])
      if (next.has(field)) next.delete(field)
      else next.add(field)
      return { ...current, [itemId]: next }
    })
  }

  function toggleOpen(itemId: string) {
    setOpen((current) => {
      const next = new Set(current)
      if (next.has(itemId)) next.delete(itemId)
      else next.add(itemId)
      return next
    })
  }

  async function runApply() {
    if (!jobId) return
    setError(null)
    setProgress([])
    setApplying(true)
    abort.current = new AbortController()
    const selections: Record<string, string[]> = {}
    for (const [itemId, fields] of Object.entries(chosen)) {
      selections[itemId] = [...fields]
    }
    try {
      await api.stream<BulkEvent>(
        '/api/bulk/apply',
        { job_id: jobId, selections, confirm: true },
        (event) => {
          if (event.type === 'summary' && event.batch_id) setBatchId(event.batch_id)
          if (event.type !== 'item' && event.type !== 'done') {
            setProgress((current) => [...current, event])
          }
          if (event.type === 'error') setError(new Error(event.message))
        },
        abort.current.signal,
      )
    } catch (caught) {
      setError(caught)
    } finally {
      setApplying(false)
      // The library changed, so anything derived from it is stale.
      void queryClient.invalidateQueries({ queryKey: ['items'] })
      void queryClient.invalidateQueries({ queryKey: ['candidates'] })
    }
  }

  const applicable = (reviewed ?? []).filter((item) => item.applicable)
  const totalFields = Object.values(chosen).reduce((sum, fields) => sum + fields.size, 0)
  const itemsWithFields = Object.values(chosen).filter((fields) => fields.size > 0).length

  const applied = progress.filter((event) => event.type === 'applied')
  const failed = progress.filter((event) => event.type === 'failed')
  const summary = progress.find((event) => event.type === 'summary')

  return (
    <section>
      <p className="small">
        <Link to={`/?kind=${kind}`}>← Library</Link>
      </p>
      <h2>Review</h2>
      <p className="muted small">
        {ids.length} selected item{ids.length === 1 ? '' : 's'} · tick exactly what should
        be written. Nothing is written to Jellyfin until you confirm, and a copy of the
        current metadata is stored before every write so it can be undone.
      </p>

      {archiveReady.data && archiveReady.data.derived.total === 0 && (
        <p className="badge warn">
          The archive is empty. Go back and fetch Last.fm data for these items first —
          there is nothing to propose yet.
        </p>
      )}

      {reviewed === null && (
        <div className="panel actions">
          <button className="primary" onClick={runDiff} disabled={diffing || ids.length === 0}>
            {diffing ? 'Comparing…' : `Compare ${ids.length} item${ids.length === 1 ? '' : 's'} with Last.fm`}
          </button>
          <span className="muted small">
            Comparing reads the local archive and the current Jellyfin state. It writes
            nothing.
          </span>
        </div>
      )}

      {error != null && <ErrorNote error={error} />}

      {reviewed !== null && (
        <>
          <div className="panel">
            <h3>
              Proposed changes{' '}
              <span className="muted small">
                {applicable.length} actionable · {reviewed.length - applicable.length} skipped
              </span>
            </h3>
            <div className="review-list">
              {reviewed.map((item) => {
                const fields = chosen[item.item_id] ?? new Set<string>()
                const isOpen = open.has(item.item_id)
                const writable = item.diff.changes.filter((change) => change.changes_anything)
                return (
                  <div className="review-item" key={item.item_id}>
                    <div className="review-head">
                      <input
                        type="checkbox"
                        aria-label={`Select ${item.name}`}
                        checked={fields.size > 0}
                        disabled={!item.applicable}
                        onChange={() => {
                          const all = writable.map((change) => change.field)
                          setChosen((current) => ({
                            ...current,
                            [item.item_id]:
                              fields.size > 0 ? new Set<string>() : new Set(all),
                          }))
                        }}
                      />
                      <button className="link" onClick={() => toggleOpen(item.item_id)}>
                        {isOpen ? '▾' : '▸'} {item.name}
                      </button>
                      <span className="badge">{item.kind.replace('Music', '').toLowerCase()}</span>
                      <ConfidenceBadge confidence={item.diff.confidence} />
                      <span className="muted small">
                        {fields.size} of {writable.length} changes selected
                      </span>
                      {item.skipped_reason && (
                        <span className="badge warn" title={item.skipped_reason}>
                          skipped: {item.skipped_reason}
                        </span>
                      )}
                      <Link className="muted small" to={`/edit/${item.item_id}`}>
                        open alone
                      </Link>
                    </div>

                    {isOpen && (
                      <div className="changes">
                        {item.diff.changes.map((change) => (
                          <ChangeRow
                            key={change.field}
                            change={change}
                            checked={fields.has(change.field)}
                            onToggle={(field) => toggleField(item.item_id, field)}
                          />
                        ))}
                        {item.diff.withheld.length > 0 && (
                          <>
                            <h4>Withheld</h4>
                            {item.diff.withheld.map((change) => (
                              <WithheldRow key={change.field} change={change} />
                            ))}
                          </>
                        )}
                      </div>
                    )}
                  </div>
                )
              })}
            </div>
          </div>

          <div className="panel actions">
            <button className="pill" onClick={() => setOpen(new Set(reviewed.map((i) => i.item_id)))}>
              Expand all
            </button>
            <button
              className="pill"
              onClick={() => {
                const next: Record<string, Set<string>> = {}
                for (const item of reviewed) {
                  next[item.item_id] = new Set(
                    item.diff.changes
                      .filter((change) => change.changes_anything)
                      .map((change) => change.field),
                  )
                }
                setChosen(next)
              }}
            >
              Select every change
            </button>
            <button
              className="pill"
              onClick={() => setChosen(Object.fromEntries(reviewed.map((i) => [i.item_id, new Set<string>()])))}
            >
              Clear all
            </button>
            <button
              className="primary"
              onClick={runApply}
              disabled={applying || !jobId || totalFields === 0}
            >
              {applying
                ? 'Writing…'
                : `Back up and write ${totalFields} field${totalFields === 1 ? '' : 's'} across ${itemsWithFields} item${itemsWithFields === 1 ? '' : 's'}`}
            </button>
            <span className="muted small">
              Each item is backed up, then written. A failure on one item does not stop the
              rest, and every write shares a batch you can undo as a unit.
            </span>
          </div>

          {summary && (
            <div className="panel">
              <h3>Result</h3>
              <p>
                <span className="badge ok">{applied.length} written</span>
                {failed.length > 0 && <span className="badge danger">{failed.length} failed</span>}
                {'skipped' in summary && summary.skipped !== undefined && (
                  <span className="badge">{summary.skipped} skipped</span>
                )}
                {batchId && <span className="muted small"> · batch {batchId.slice(0, 8)}</span>}
              </p>
              <table className="grid">
                <thead>
                  <tr>
                    <th>Item</th>
                    <th>Outcome</th>
                  </tr>
                </thead>
                <tbody>
                  {progress
                    .filter((event) => event.type === 'applied' || event.type === 'failed')
                    .map((event, index) => (
                      <tr key={`${event.type}-${index}`}>
                        <td>
                          {event.type === 'applied' ? event.name : (event.name ?? event.item_id)}
                          <span className="muted small"> {event.item_id.slice(0, 8)}</span>
                        </td>
                        <td>
                          {event.type === 'applied' ? (
                            <>
                              <span className="badge ok">written</span>
                              <span className="muted small">
                                {' '}
                                {event.applied_fields.join(', ') || 'no change'}
                              </span>
                            </>
                          ) : (
                            <>
                              <span className="badge danger">failed</span>
                              <span className="muted small"> {event.error}</span>
                            </>
                          )}
                        </td>
                      </tr>
                    ))}
                </tbody>
              </table>
              {failed.length > 0 && (
                <p className="muted small">
                  The items that succeeded are still written and still revertible. Re-run
                  the comparison to try the failures again.
                </p>
              )}
            </div>
          )}
        </>
      )}
    </section>
  )
}
