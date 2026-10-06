import { useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import {
  api,
  type BulkEvent,
  type BulkJobSummary,
  type BulkJobListResponse,
  type DiffResponse,
  type SelectionKind,
} from '../api/client'
import { ConfidenceBadge, ErrorNote } from '../components/ui'

const KINDS: { value: SelectionKind; label: string }[] = [
  { value: 'artist', label: 'Artists' },
  { value: 'album', label: 'Albums' },
  { value: 'song', label: 'Songs' },
]

interface ReviewedItem {
  item_id: string
  name: string
  applicable: boolean
  skipped_reason: string | null
  diff: DiffResponse
}

/**
 * Bulk editing, in the order that makes it safe: review, then apply, then (if needed)
 * revert the whole batch.
 *
 * The diff is a prerequisite rather than a courtesy -- the server issues a job id that
 * the apply must present, so a library-wide write cannot be the first thing a session
 * does. Progress arrives as Server-Sent Events, so a long run reports each item as it
 * happens rather than at the end.
 */
export function Bulk() {
  const queryClient = useQueryClient()
  const [kind, setKind] = useState<SelectionKind>('artist')
  const [limit, setLimit] = useState(25)
  const [missingOnly, setMissingOnly] = useState(true)
  const [minConfidence, setMinConfidence] = useState(0)

  const [reviewed, setReviewed] = useState<ReviewedItem[] | null>(null)
  const [jobId, setJobId] = useState<string | null>(null)
  const [diffing, setDiffing] = useState(false)
  const [applying, setApplying] = useState(false)
  const [applied, setApplied] = useState<BulkEvent[]>([])
  const [batchId, setBatchId] = useState<string | null>(null)
  const [reverting, setReverting] = useState(false)
  const [reverted, setReverted] = useState<BulkEvent[]>([])
  const [error, setError] = useState<unknown>(null)

  const jobs = useQuery({
    queryKey: ['bulk-jobs'],
    queryFn: () => api.get<BulkJobListResponse>('/api/bulk/jobs'),
  })

  const abort = useRef<AbortController | null>(null)

  async function runDiff() {
    setError(null)
    setReviewed([])
    setApplied([])
    setReverted([])
    setBatchId(null)
    setJobId(null)
    setDiffing(true)
    abort.current = new AbortController()
    const collected: ReviewedItem[] = []
    try {
      await api.stream<BulkEvent>(
        '/api/bulk/diff',
        {
          selection: { kind, limit, missing_metadata: missingOnly },
          min_confidence: minConfidence,
        },
        (event) => {
          if (event.type === 'item') {
            collected.push({
              item_id: event.item_id,
              name: event.name,
              applicable: event.applicable,
              skipped_reason: event.skipped_reason,
              diff: event.diff,
            })
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
    } catch (caught) {
      setError(caught)
    } finally {
      setDiffing(false)
      void queryClient.invalidateQueries({ queryKey: ['bulk-jobs'] })
    }
  }

  async function runApply() {
    if (!jobId) return
    setError(null)
    setApplied([])
    setApplying(true)
    abort.current = new AbortController()
    try {
      await api.stream<BulkEvent>(
        '/api/bulk/apply',
        { job_id: jobId, confirm: true },
        (event) => {
          if (event.type === 'summary') {
            if (event.batch_id) setBatchId(event.batch_id)
          }
          if (event.type !== 'item' && event.type !== 'done') {
            setApplied((current) => [...current, event])
          }
          if (event.type === 'error') setError(new Error(event.message))
        },
        abort.current.signal,
      )
    } catch (caught) {
      setError(caught)
    } finally {
      setApplying(false)
      void queryClient.invalidateQueries({ queryKey: ['bulk-jobs'] })
      void queryClient.invalidateQueries({ queryKey: ['items'] })
    }
  }

  async function runRevert() {
    if (!batchId) return
    setError(null)
    setReverted([])
    setReverting(true)
    abort.current = new AbortController()
    try {
      await api.stream<BulkEvent>(
        `/api/bulk/${batchId}/revert?confirm=true`,
        {},
        (event) => {
          if (event.type !== 'item' && event.type !== 'done') {
            setReverted((current) => [...current, event])
          }
          if (event.type === 'error') setError(new Error(event.message))
        },
        abort.current.signal,
      )
    } catch (caught) {
      setError(caught)
    } finally {
      setReverting(false)
      void queryClient.invalidateQueries({ queryKey: ['items'] })
    }
  }

  const applicable = (reviewed ?? []).filter((item) => item.applicable).length
  const skipped = (reviewed ?? []).length - applicable

  return (
    <section>
      <h2>Bulk</h2>
      <p className="muted small">
        Review first. The reviewed set becomes a job, and only that job can be applied —
        which is why a bulk write can never be the first thing this page does.
      </p>

      <div className="panel">
        <div className="row">
          <div className="pill-row">
            {KINDS.map((entry) => (
              <button
                key={entry.value}
                className={entry.value === kind ? 'pill active' : 'pill'}
                onClick={() => setKind(entry.value)}
              >
                {entry.label}
              </button>
            ))}
          </div>
          <label className="check">
            <input
              type="checkbox"
              checked={missingOnly}
              onChange={(event) => setMissingOnly(event.target.checked)}
            />
            only items missing metadata
          </label>
          <label className="check">
            limit
            <input
              type="number"
              min={1}
              max={500}
              value={limit}
              onChange={(event) => setLimit(Number(event.target.value))}
              style={{ width: '5rem' }}
            />
          </label>
          <label className="check">
            min confidence
            <input
              type="number"
              min={0}
              max={1}
              step={0.05}
              value={minConfidence}
              onChange={(event) => setMinConfidence(Number(event.target.value))}
              style={{ width: '5rem' }}
            />
          </label>
          <button className="primary" onClick={runDiff} disabled={diffing}>
            {diffing ? 'Reviewing…' : 'Review'}
          </button>
        </div>
        <p className="muted small">
          An item whose best match is not trustworthy is reported as skipped, never
          guessed at. Reviewing writes nothing.
        </p>
      </div>

      {error != null && <ErrorNote error={error} />}

      {reviewed && (
        <div className="panel">
          <h3>
            Reviewed <span className="muted small">
              {reviewed.length} items · {applicable} applicable · {skipped} skipped
            </span>
          </h3>
          <table className="grid">
            <thead>
              <tr>
                <th>Item</th>
                <th>Confidence</th>
                <th>Would change</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {reviewed.map((item) => (
                <tr key={item.item_id}>
                  <td>
                    <Link to={`/edit/${item.item_id}`}>{item.name}</Link>
                  </td>
                  <td>
                    <ConfidenceBadge confidence={item.diff.confidence} />
                  </td>
                  <td className="chips">
                    {item.diff.default_selection.length === 0 ? (
                      <span className="muted">—</span>
                    ) : (
                      item.diff.default_selection.map((field) => (
                        <span className="chip" key={field}>
                          {field}
                        </span>
                      ))
                    )}
                  </td>
                  <td>
                    {item.skipped_reason ? (
                      <span className="badge warn" title={item.skipped_reason}>
                        skipped
                      </span>
                    ) : (
                      <span className="badge ok">applicable</span>
                    )}
                    {item.skipped_reason && (
                      <span className="muted small"> {item.skipped_reason}</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

          <div className="actions">
            <button
              className="primary"
              onClick={runApply}
              disabled={applying || !jobId || applicable === 0}
            >
              {applying ? 'Applying…' : `Apply to ${applicable} items`}
            </button>
            <span className="muted small">
              Applies each item's default selection. A failure on one item does not stop
              the rest, and everything applied shares a batch you can revert as a unit.
            </span>
          </div>
        </div>
      )}

      {applied.length > 0 && (
        <div className="panel">
          <h3>Applied</h3>
          <EventLog events={applied} />
          {batchId && (
            <div className="actions">
              <button className="pill" onClick={runRevert} disabled={reverting}>
                {reverting ? 'Reverting…' : 'Revert this batch'}
              </button>
              <span className="muted small">batch {batchId.slice(0, 8)}</span>
            </div>
          )}
        </div>
      )}

      {reverted.length > 0 && (
        <div className="panel">
          <h3>Reverted</h3>
          <EventLog events={reverted} />
        </div>
      )}

      {jobs.data && jobs.data.count > 0 && (
        <div className="panel">
          <h3>Reviewed batches</h3>
          <table className="grid">
            <thead>
              <tr>
                <th>Job</th>
                <th>Items</th>
                <th>Applicable</th>
                <th>Created</th>
                <th>Applied</th>
              </tr>
            </thead>
            <tbody>
              {jobs.data.jobs.map((job: BulkJobSummary) => (
                <tr key={job.job_id}>
                  <td className="mono small">{job.job_id.slice(0, 8)}</td>
                  <td>{job.items}</td>
                  <td>{job.applicable}</td>
                  <td className="muted small">
                    {new Date(job.created_at).toLocaleTimeString()}
                  </td>
                  <td>
                    {job.applied ? (
                      <span className="badge ok">yes</span>
                    ) : (
                      <span className="badge">no</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}

function EventLog({ events }: { events: BulkEvent[] }) {
  return (
    <ul className="event-log">
      {events.map((event, index) => {
        if (event.type === 'applied') {
          return (
            <li key={`${event.item_id}-${index}`} className="ok">
              {event.name} — {event.applied_fields.join(', ') || 'no changes'}
            </li>
          )
        }
        if (event.type === 'reverted') {
          return (
            <li key={`${event.item_id}-${index}`} className="ok">
              {event.item_id.slice(0, 8)} — restored {event.restored_fields.join(', ')}
            </li>
          )
        }
        if (event.type === 'failed') {
          return (
            <li key={`${event.item_id}-${index}`} className="danger">
              {event.name ?? event.item_id} — {event.error}
            </li>
          )
        }
        if (event.type === 'summary') {
          return (
            <li key={`summary-${index}`} className="muted">
              {event.applied !== undefined && `applied ${event.applied}, `}
              {event.reverted !== undefined && `reverted ${event.reverted}, `}
              failed {event.failed ?? 0}, skipped {event.skipped ?? 0}
            </li>
          )
        }
        if (event.type === 'error') {
          return (
            <li key={`error-${index}`} className="danger">
              {event.code}: {event.message}
            </li>
          )
        }
        return null
      })}
    </ul>
  )
}
