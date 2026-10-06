import { useMemo, useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  api,
  type ApplyResponse,
  type CandidateView,
  type CandidatesResponse,
  type DiffResponse,
  type SnapshotSummary,
  type SnapshotListResponse,
} from '../api/client'
import { ChangeRow, ConfidenceBadge, ConfidenceDetail, ErrorNote, WithheldRow } from '../components/ui'

/**
 * The editor: pick a candidate, review the diff, write the fields you chose.
 *
 * The three steps are separate screens on purpose. A write is only reachable after the
 * diff has been rendered, and the diff only after a specific candidate has been named,
 * so there is no path from "look up this item" to "overwrite it" that skips review.
 */
export function Editor() {
  const { itemId = '' } = useParams()
  const [params] = useSearchParams()
  const queryClient = useQueryClient()

  const [entityId, setEntityId] = useState<number | null>(null)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [outcome, setOutcome] = useState<ApplyResponse | null>(null)

  const candidates = useQuery({
    queryKey: ['candidates', itemId],
    queryFn: () => api.post<CandidatesResponse>(`/api/items/${itemId}/candidates`, {}),
    enabled: Boolean(itemId),
  })

  // Default to the best candidate, but never silently: the choice stays visible.
  const chosen = useMemo(() => {
    if (entityId !== null) return entityId
    return candidates.data?.candidates[0]?.entity_id ?? null
  }, [entityId, candidates.data])

  const diff = useQuery({
    queryKey: ['diff', itemId, chosen],
    queryFn: () => api.post<DiffResponse>(`/api/items/${itemId}/diff`, { entity_id: chosen }),
    enabled: chosen !== null,
  })

  const snapshots = useQuery({
    queryKey: ['snapshots', itemId],
    queryFn: () => api.get<SnapshotListResponse>(`/api/items/${itemId}/snapshots`),
    enabled: Boolean(itemId),
  })

  const apply = useMutation({
    mutationFn: (fields: string[]) =>
      api.post<ApplyResponse>(`/api/items/${itemId}/apply`, {
        entity_id: chosen,
        fields,
        confirm: true,
        // The etag the diff was prepared against, so an edit made in Jellyfin
        // meanwhile is refused rather than overwritten.
        expected_etag: diff.data?.etag ?? null,
      }),
    onSuccess: (result) => {
      setOutcome(result)
      setSelected(new Set())
      void queryClient.invalidateQueries({ queryKey: ['snapshots', itemId] })
      void queryClient.invalidateQueries({ queryKey: ['candidates', itemId] })
      void queryClient.invalidateQueries({ queryKey: ['diff', itemId] })
      void queryClient.invalidateQueries({ queryKey: ['items'] })
    },
  })

  const revert = useMutation({
    mutationFn: (snapshotId: number) =>
      api.post<ApplyResponse>(`/api/snapshots/${snapshotId}/revert?confirm=true`),
    onSuccess: (result) => {
      setOutcome(result)
      void queryClient.invalidateQueries({ queryKey: ['snapshots', itemId] })
      void queryClient.invalidateQueries({ queryKey: ['items'] })
    },
  })

  function toggle(field: string) {
    setSelected((current) => {
      const next = new Set(current)
      if (next.has(field)) next.delete(field)
      else next.add(field)
      return next
    })
  }

  const writable = (diff.data?.changes ?? []).filter((change) => change.changes_anything)

  return (
    <section>
      <p className="small">
        <Link to={`/?kind=${params.get('kind') ?? 'artist'}`}>← Library</Link>
      </p>
      <h2>{candidates.data?.name ?? 'Loading…'}</h2>
      {candidates.data && (
        <p className="muted small">
          {candidates.data.kind}
          {candidates.data.etag ? ` · etag ${candidates.data.etag.slice(0, 8)}` : ''} ·{' '}
          {candidates.data.count} archived candidate
          {candidates.data.count === 1 ? '' : 's'}
        </p>
      )}
      {candidates.isError && <ErrorNote error={candidates.error} />}

      {candidates.data?.count === 0 && (
        <p className="badge warn">
          No archived Last.fm data matches this item yet. Nothing can be proposed until it
          has been fetched, and the archive is read-only here by design.
        </p>
      )}

      {candidates.data && candidates.data.candidates.length > 0 && (
        <div className="panel">
          <h3>Candidate</h3>
          <ul className="candidate-list">
            {candidates.data.candidates.map((candidate) => (
              <li
                key={candidate.entity_id}
                className={candidate.entity_id === chosen ? 'candidate active' : 'candidate'}
              >
                <label>
                  <input
                    type="radio"
                    name="candidate"
                    checked={candidate.entity_id === chosen}
                    onChange={() => {
                      setEntityId(candidate.entity_id)
                      setSelected(new Set())
                      setOutcome(null)
                    }}
                  />
                  <strong>{candidate.name}</strong>
                  {candidate.artist && <span className="muted"> — {candidate.artist}</span>}
                  <ConfidenceBadge confidence={candidate.confidence} />
                  <span className="badge">{candidate.matched_on}</span>
                </label>
                <CandidateDetails candidate={candidate} />
              </li>
            ))}
          </ul>
        </div>
      )}

      {diff.data && (
        <>
          <div className="panel">
            <h3>
              Proposed changes{' '}
              <span className="muted small">
                {diff.data.changes.length} proposed · {diff.data.withheld.length} withheld
              </span>
            </h3>
            <ConfidenceDetail confidence={diff.data.confidence} />
            {diff.data.locked_fields.length > 0 && (
              <p className="badge warn">
                Jellyfin has these fields locked, so they cannot be written:{' '}
                {diff.data.locked_fields.join(', ')}
              </p>
            )}
            <div className="changes">
              {diff.data.changes.map((change) => (
                <ChangeRow
                  key={change.field}
                  change={change}
                  checked={selected.has(change.field)}
                  onToggle={toggle}
                  disabled={!writable.includes(change)}
                />
              ))}
              {diff.data.changes.length === 0 && (
                <p className="muted">
                  Nothing to propose — this item already matches the candidate.
                </p>
              )}
            </div>
          </div>

          {diff.data.withheld.length > 0 && (
            <div className="panel">
              <h3>Withheld</h3>
              <p className="muted small">
                Deliberately not proposed. A withheld field is not a failure: it is the
                policy refusing to write a value it cannot justify.
              </p>
              <div className="changes">
                {diff.data.withheld.map((change) => (
                  <WithheldRow key={change.field} change={change} />
                ))}
              </div>
            </div>
          )}

          <div className="panel actions">
            <button
              className="pill"
              onClick={() => setSelected(new Set(writable.map((change) => change.field)))}
              disabled={writable.length === 0}
            >
              Select all {writable.length} changed
            </button>
            <button className="pill" onClick={() => setSelected(new Set())}>
              Clear
            </button>
            <button
              className="primary"
              disabled={selected.size === 0 || apply.isPending}
              onClick={() => apply.mutate([...selected])}
            >
              {apply.isPending
                ? 'Applying…'
                : `Apply ${selected.size} field${selected.size === 1 ? '' : 's'}`}
            </button>
            <span className="muted small">
              Only the ticked fields are written; every other field is sent back unchanged.
            </span>
          </div>
          {apply.isError && <ErrorNote error={apply.error} />}
        </>
      )}

      {outcome && (
        <div className="panel">
          <h3>Result</h3>
          <p>
            <span className="badge ok">applied</span>{' '}
            {outcome.applied.length > 0 ? outcome.applied.join(', ') : 'no changes'}
            {outcome.snapshot_id !== null && (
              <span className="muted small"> · snapshot {outcome.snapshot_id}</span>
            )}{' '}
            <span className="muted small">{outcome.duration_ms} ms</span>
          </p>
          {outcome.etag_before !== outcome.etag_after && (
            <p className="muted small">
              etag {outcome.etag_before?.slice(0, 8)} → {outcome.etag_after?.slice(0, 8)}
            </p>
          )}
        </div>
      )}

      {snapshots.data && snapshots.data.snapshots.length > 0 && (
        <div className="panel">
          <h3>History</h3>
          <table className="grid">
            <thead>
              <tr>
                <th>#</th>
                <th>Operation</th>
                <th>When</th>
                <th>Fields</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {snapshots.data.snapshots.map((snapshot) => (
                <SnapshotRow
                  key={snapshot.id}
                  snapshot={snapshot}
                  onRevert={() => revert.mutate(snapshot.id)}
                  busy={revert.isPending}
                />
              ))}
            </tbody>
          </table>
          {revert.isError && <ErrorNote error={revert.error} />}
        </div>
      )}
    </section>
  )
}

function CandidateDetails({ candidate }: { candidate: CandidateView }) {
  return (
    <div className="candidate-detail">
      {candidate.tags.length > 0 && (
        <div className="chips">
          {candidate.tags.slice(0, 8).map((tag) => (
            <span className="chip" key={tag.name}>
              {tag.name}
              {tag.count !== null && <span className="muted small"> {tag.count}</span>}
            </span>
          ))}
        </div>
      )}
      <p className="muted small">
        {[
          candidate.mbid ? `mbid ${candidate.mbid.slice(0, 8)}` : 'no mbid',
          candidate.listeners !== null ? `${candidate.listeners} listeners` : null,
          candidate.has_overview ? 'has overview' : 'no overview',
          candidate.url ? null : 'no url',
        ]
          .filter(Boolean)
          .join(' · ')}
      </p>
      {candidate.confidence.notes.length > 0 && (
        <p className="muted small">{candidate.confidence.notes.join(' · ')}</p>
      )}
    </div>
  )
}

function SnapshotRow({
  snapshot,
  onRevert,
  busy,
}: {
  snapshot: SnapshotSummary
  onRevert: () => void
  busy: boolean
}) {
  return (
    <tr>
      <td className="mono">{snapshot.id}</td>
      <td>
        <span className="badge">{snapshot.source_op}</span>
      </td>
      <td className="muted small">
        {snapshot.created_at ? new Date(snapshot.created_at).toLocaleString() : '—'}
      </td>
      <td className="muted small">{snapshot.field_count} fields</td>
      <td>
        <button className="pill" onClick={onRevert} disabled={busy}>
          revert
        </button>
      </td>
    </tr>
  )
}
