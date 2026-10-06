import { useQuery } from '@tanstack/react-query'
import { api } from '../api/client'

export interface ArchiveStats {
  payload_bytes: number
  cap_bytes: number
  warn_bytes: number
  used_ratio: number
  headroom_bytes: number
  state: 'ok' | 'warning' | 'cap_reached'
  response_rows: number
  observations: number
  request_rows: number
  oldest_request_at: string | null
}

export function bytes(value: number): string {
  if (value < 1024) return `${value} B`
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KiB`
  return `${(value / (1024 * 1024)).toFixed(2)} MiB`
}

const stateBadge = { ok: 'ok', warning: 'warn', cap_reached: 'danger' } as const

/**
 * The local Last.fm dataset as a first-class view: how much of the ToS storage
 * cap we are using, and what we have accumulated.
 */
export function Archive() {
  const stats = useQuery({
    queryKey: ['archive-stats'],
    queryFn: () => api.get<ArchiveStats>('/api/archive/stats'),
    retry: false,
  })

  if (stats.isLoading) return <div className="panel">Loading archive statistics…</div>
  if (stats.isError || !stats.data) {
    return (
      <div className="panel">
        <h2>Archive</h2>
        <p className="badge warn">unavailable</p>
        <p className="muted mono">{(stats.error as Error | null)?.message ?? 'no data'}</p>
      </div>
    )
  }

  const data = stats.data
  const percent = Math.min(100, Math.round(data.used_ratio * 100))

  return (
    <>
      <div className="panel">
        <h2>
          Archive capacity <span className={`badge ${stateBadge[data.state]}`}>{data.state}</span>
        </h2>
        <p>
          {bytes(data.payload_bytes)} of {bytes(data.cap_bytes)} stored Last.fm payload (
          {percent}%), {bytes(data.headroom_bytes)} headroom.
        </p>
        <div
          style={{
            height: '0.6rem',
            background: 'var(--border)',
            borderRadius: '999px',
            overflow: 'hidden',
          }}
        >
          <div
            style={{
              width: `${percent}%`,
              height: '100%',
              background: data.state === 'ok' ? 'var(--ok)' : data.state === 'warning' ? 'var(--warn)' : 'var(--danger)',
            }}
          />
        </div>
        <p className="muted" style={{ fontSize: '0.85rem' }}>
          The Last.fm API Terms of Service cap stored Last.fm Data at 100 MB. Bytes are measured and
          shown here; nothing is ever deleted automatically.
        </p>
      </div>

      <div className="panel">
        <h2>Contents</h2>
        <div className="grid">
          <div>
            <div className="muted">Distinct response bodies</div>
            <div>{data.response_rows.toLocaleString()}</div>
          </div>
          <div>
            <div className="muted">Observations logged</div>
            <div>{data.observations.toLocaleString()}</div>
          </div>
          <div>
            <div className="muted">Requests recorded</div>
            <div>{data.request_rows.toLocaleString()}</div>
          </div>
          <div>
            <div className="muted">Oldest observation</div>
            <div className="mono">{data.oldest_request_at ?? '—'}</div>
          </div>
        </div>
        <p className="muted" style={{ fontSize: '0.85rem' }}>
          Identical response bodies are stored once; every request against them is still logged, which
          is why observations normally exceed distinct bodies.
        </p>
      </div>
    </>
  )
}
