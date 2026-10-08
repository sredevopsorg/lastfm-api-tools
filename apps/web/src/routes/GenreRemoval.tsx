import { useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import {
  api,
  type GenreRemovalRequest,
  type GenreVocabularyResponse,
  type RemovalEvent,
  type RemovalField,
  type RemovalJobSummary,
  type RemovalJobListResponse,
  type RemovalOutcome,
  type SelectionKind,
} from '../api/client'
import { ErrorNote, Value } from '../components/ui'
import { EMPTY_FACETS, SelectionFilters } from '../components/facet_controls'

const KINDS: { value: SelectionKind; label: string }[] = [
  { value: 'artist', label: 'Artists' },
  { value: 'album', label: 'Albums' },
  { value: 'song', label: 'Songs' },
]

const FIELDS: RemovalField[] = ['Genres', 'Tags']

interface Target {
  item_id: string
  name: string
  applicable: boolean
  skipped_reason: string | null
  removals: RemovalOutcome[]
}

const EMPTY_FORM = {
  kind: 'artist' as SelectionKind,
  genre: '',
  fields: ['Genres'] as RemovalField[],
  decompose: false,
  limit: 100,
  search: '',
  facets: EMPTY_FACETS,
}

/**
 * Remove one genre from every item carrying it.
 *
 * The same review-then-apply-then-revert shape as Bulk, deliberately: this is a bulk
 * operation over a whole library, so it uses the one bulk idiom the app already has rather
 * than inventing a second one. The server enforces the ordering — the apply must present a
 * job id from a diff — so the confirmation step is a property of the API, not of this
 * component.
 *
 * The genre is chosen from the library's own vocabulary where possible. Typing it free-hand
 * is allowed, because Jellyfin's entity list can lag behind item values, but the datalist
 * is what stops a typo becoming a silent no-op: an exact match that matches nothing reports
 * "0 items" rather than looking broken.
 */
export function GenreRemoval() {
  const queryClient = useQueryClient()
  const [form, setForm] = useState(EMPTY_FORM)
  const [reviewed, setReviewed] = useState<Target[] | null>(null)
  const [jobId, setJobId] = useState<string | null>(null)
  const [batchId, setBatchId] = useState<string | null>(null)
  const [diffing, setDiffing] = useState(false)
  const [applying, setApplying] = useState(false)
  const [applied, setApplied] = useState<RemovalEvent[]>([])
  const [reverted, setReverted] = useState<RemovalEvent[]>([])
  const [emptied, setEmptied] = useState<string[]>([])
  const [error, setError] = useState<unknown>(null)
  const abort = useRef<AbortController | null>(null)

  const vocabulary = useQuery({
    queryKey: ['genres', form.kind],
    queryFn: () =>
      api.get<GenreVocabularyResponse>(
        `/api/bulk/genres?item_kind=${itemKindFor(form.kind)}`,
      ),
  })

  const jobs = useQuery({
    queryKey: ['removal-jobs'],
    queryFn: () => api.get<RemovalJobListResponse>('/api/bulk/remove-genre/jobs'),
  })

  const body = useMemo<GenreRemovalRequest>(
    () => ({
      genre: form.genre,
      fields: form.fields,
      decompose: form.decompose,
      selection: {
        kind: form.kind,
        limit: form.limit,
        ...(form.search ? { search: form.search } : {}),
        ...(form.facets.artistIds.length ? { artist_ids: form.facets.artistIds } : {}),
        ...(form.facets.albumIds.length ? { album_ids: form.facets.albumIds } : {}),
        ...(form.facets.patterns.length ? { exclude: form.facets.patterns } : {}),
      },
    }),
    [form],
  )

  async function runDiff() {
    setError(null)
    setReviewed([])
    setApplied([])
    setReverted([])
    setEmptied([])
    setJobId(null)
    setBatchId(null)
    setDiffing(true)
    abort.current = new AbortController()
    const collected: Target[] = []
    try {
      await api.stream<RemovalEvent>(
        '/api/bulk/remove-genre/diff',
        body,
        (event) => {
          if (event.type === 'item') {
            collected.push({
              item_id: event.item_id,
              name: event.name,
              applicable: event.applicable,
              skipped_reason: event.skipped_reason,
              // Optional on the wire, because the same frame shape serves both a Last.fm
              // diff and a removal. Defaulting here keeps the table's own type total, so
              // nothing downstream has to handle "removals may be absent" as well.
              removals: event.removals ?? [],
            })
            setReviewed([...collected])
          } else if (event.type === 'summary') {
            setJobId(event.job_id ?? null)
            setBatchId(event.batch_id ?? null)
            setEmptied((event.emptied ?? []).map((entry) => `${entry.name} (${entry.field})`))
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
      void queryClient.invalidateQueries({ queryKey: ['removal-jobs'] })
    }
  }

  async function runApply() {
    if (!jobId) return
    setError(null)
    setApplied([])
    setApplying(true)
    abort.current = new AbortController()
    try {
      await api.stream<RemovalEvent>(
        '/api/bulk/remove-genre/apply',
        { job_id: jobId, confirm: true },
        (event) => {
          if (event.type === 'summary' && event.batch_id) setBatchId(event.batch_id)
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
      void queryClient.invalidateQueries({ queryKey: ['removal-jobs'] })
      void queryClient.invalidateQueries({ queryKey: ['items'] })
    }
  }

  async function runRevert() {
    if (!batchId) return
    setError(null)
    setReverted([])
    abort.current = new AbortController()
    try {
      await api.stream<RemovalEvent>(
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
      void queryClient.invalidateQueries({ queryKey: ['items'] })
    }
  }

  const applicable = (reviewed ?? []).filter((item) => item.applicable).length
  const removedTotal = (reviewed ?? []).reduce(
    (total, item) => total + item.removals.reduce((sum, r) => sum + r.removed.length, 0),
    0,
  )
  const target = form.genre.trim()

  return (
    <section>
      <h2>Remove a genre</h2>
      <p className="muted small">
        Removes one genre from every item that carries it. Review first: the reviewed set
        becomes a job, and only that job can be applied. Every write is snapshotted, so the
        whole run reverts as a batch.
      </p>

      <div className="panel">
        <div className="row">
          <div className="pill-row">
            {KINDS.map((entry) => (
              <button
                key={entry.value}
                className={entry.value === form.kind ? 'pill active' : 'pill'}
                onClick={() => setForm({ ...form, kind: entry.value })}
              >
                {entry.label}
              </button>
            ))}
          </div>
        </div>

        <div className="row">
          {/* `htmlFor` rather than wrapping the input, and the visible word is "genre to
              remove" rather than "genre": the page heading is "Remove a genre", so a label
              of "genre" is ambiguous to a screen reader and to a test locator alike. */}
          <label className="check" htmlFor="removal-genre">
            genre to remove
          </label>
          <input
            id="removal-genre"
            list="library-genres"
            value={form.genre}
            placeholder="e.g. Gothic Rock"
            onChange={(event) => setForm({ ...form, genre: event.target.value })}
            style={{ minWidth: '16rem' }}
          />
          <datalist id="library-genres">
            {(vocabulary.data?.genres ?? []).map((genre) => (
              <option key={genre} value={genre} />
            ))}
          </datalist>

          {FIELDS.map((field) => (
            <label className="check" key={field}>
              <input
                type="checkbox"
                checked={form.fields.includes(field)}
                onChange={() =>
                  setForm({
                    ...form,
                    fields: form.fields.includes(field)
                      ? form.fields.filter((entry) => entry !== field)
                      : [...form.fields, field],
                  })
                }
              />
              {field}
            </label>
          ))}

          <label className="check">
            search
            <input
              value={form.search}
              placeholder="optional"
              onChange={(event) => setForm({ ...form, search: event.target.value })}
              style={{ width: '9rem' }}
            />
          </label>
          <label className="check">
            limit
            <input
              type="number"
              min={1}
              max={500}
              value={form.limit}
              onChange={(event) => setForm({ ...form, limit: Number(event.target.value) })}
              style={{ width: '5rem' }}
            />
          </label>
          <button
            className="primary"
            onClick={runDiff}
            disabled={diffing || !target || form.fields.length === 0}
          >
            {diffing ? 'Reviewing…' : 'Review'}
          </button>
        </div>

        <SelectionFilters
          kind={form.kind}
          values={form.facets}
          onChange={(facets) => setForm({ ...form, facets })}
          idPrefix="removal"
        />

        <div className="row">
          {/* Off by default and labelled with its consequence. A packed value is
              sometimes one genre whose name contains a separator, so splitting it is the
              destructive reading and must be an explicit choice. */}
          <label className="check">
            <input
              type="checkbox"
              checked={form.decompose}
              onChange={(event) => setForm({ ...form, decompose: event.target.checked })}
            />
            also split packed values
          </label>
          <span className="muted small">
            Off: only a value equal to <span className="mono">{target || 'the genre'}</span> is
            removed. On: it is also removed from a packed value like{' '}
            <span className="mono">Rock, Reggae</span>, leaving{' '}
            <span className="mono">Rock</span>. Selecting packed values needs a scan, because
            Jellyfin cannot filter by part of a value.
          </span>
        </div>

        {vocabulary.data && (
          <p className="muted small">
            {vocabulary.data.count} genre names in this library.{' '}
            {vocabulary.data.note}
          </p>
        )}
        <ErrorNote error={vocabulary.error} />
      </div>

      <ErrorNote error={error} />

      {emptied.length > 0 && (
        <p className="badge warn">
          This would leave the field empty on: {emptied.join(', ')}. Legitimate, and
          revertible, but worth a look before writing.
        </p>
      )}

      {reviewed && (
        <div className="panel">
          <h3>
            Reviewed{' '}
            <span className="muted small">
              {reviewed.length} matched · {applicable} would change · {removedTotal} value(s)
              removed
            </span>
          </h3>

          {reviewed.length === 0 ? (
            <p className="badge warn">
              Nothing in this library carries <span className="mono">{target}</span> as a
              value, so there is nothing to remove. Matching is case-insensitive and exact —
              it does not match a genre that merely contains this text.
            </p>
          ) : (
            <table className="grid">
              <thead>
                <tr>
                  <th>Item</th>
                  <th>Field</th>
                  <th>Removing</th>
                  <th>Result</th>
                </tr>
              </thead>
              <tbody>
                {reviewed.map((item) =>
                  item.removals.map((removal, index) => (
                    <tr key={`${item.item_id}-${removal.field}`}>
                      <td>
                        {index === 0 ? <Link to={`/edit/${item.item_id}`}>{item.name}</Link> : ''}
                      </td>
                      <td className="mono">{removal.field}</td>
                      <td>
                        {removal.removed.length === 0 ? (
                          <span className="muted">— nothing —</span>
                        ) : (
                          <span className="chips">
                            {removal.removed.map((value) => (
                              <span className="chip danger-chip" key={value}>
                                {value}
                              </span>
                            ))}
                          </span>
                        )}
                      </td>
                      <td>
                        <Value value={removal.after} />
                        {removal.emptied && <span className="badge warn"> emptied</span>}
                      </td>
                    </tr>
                  )),
                )}
              </tbody>
            </table>
          )}

          {reviewed.length > 0 && (
            <div className="actions">
              <button
                className="primary"
                onClick={runApply}
                disabled={applying || !jobId || applicable === 0}
              >
                {applying ? 'Applying…' : `Remove from ${applicable} item(s)`}
              </button>
              <span className="muted small">
                A failure on one item does not stop the rest, and everything applied shares a
                batch you can revert as a unit.
              </span>
            </div>
          )}
        </div>
      )}

      {applied.length > 0 && (
        <div className="panel">
          <h3>Applied</h3>
          <EventLog events={applied} />
          {batchId && (
            <div className="actions">
              <button className="pill" onClick={runRevert}>
                Revert this batch
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
          <h3>Reviewed removals</h3>
          <table className="grid">
            <thead>
              <tr>
                <th>Job</th>
                <th>Removing</th>
                <th>Items</th>
                <th>Applicable</th>
                <th>Applied</th>
              </tr>
            </thead>
            <tbody>
              {jobs.data.jobs.map((job: RemovalJobSummary) => (
                <tr key={job.job_id}>
                  <td className="mono small">{job.job_id.slice(0, 8)}</td>
                  <td className="mono">{job.removing ?? '—'}</td>
                  <td>{job.items}</td>
                  <td>{job.applicable}</td>
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

/** The Jellyfin media type for a browse kind, for the genre-vocabulary request. */
function itemKindFor(kind: SelectionKind): string {
  return { artist: 'MusicArtist', album: 'MusicAlbum', song: 'Audio' }[kind]
}

function EventLog({ events }: { events: RemovalEvent[] }) {
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
