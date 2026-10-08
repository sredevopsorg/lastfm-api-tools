import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  api,
  type ArchiveEntitiesQuery,
  type ArchiveEntityDetail,
  type ArchiveEntitySummary,
  type ArchiveStats,
  type ReindexResponse,
} from '../api/client'
import { ErrorNote, bytes } from '../components/ui'
import { Pagination, SortHeader } from '../components/table'
import type { SortState } from '../components/paging'

const stateBadge = { ok: 'ok', warning: 'warn', cap_reached: 'danger' } as const
const KINDS = ['artist', 'album', 'track'] as const

/**
 * The local Last.fm dataset as a first-class view.
 *
 * Not merely diagnostic: the archive is what lets the editor work without spending
 * rate-limited requests, and the ToS storage cap means the operator has to be able to
 * see how close they are to it. Nothing here is ever deleted automatically -- reaching
 * the cap refuses new writes rather than discarding history.
 */
export function Archive() {
  const queryClient = useQueryClient()
  const [kind, setKind] = useState<(typeof KINDS)[number]>('artist')
  const [search, setSearch] = useState('')
  const [draft, setDraft] = useState('')
  const [openId, setOpenId] = useState<number | null>(null)
  const [sortState, setSortState] = useState<SortState>({ sort: 'name', order: 'asc' })
  // The API is 1-based here and 0-based for the library browse, which is a difference
  // worth containing in one place rather than at every call site.
  const [page, setPage] = useState(1)
  const PAGE_SIZE = 50

  const stats = useQuery({
    queryKey: ['archive-stats'],
    queryFn: () => api.get<ArchiveStats>('/api/archive/stats'),
    retry: false,
  })

  const entities = useQuery({
    queryKey: ['archive-entities', kind, search, sortState.sort, sortState.order, page],
    queryFn: () =>
      api.archiveEntities({
        kind,
        page,
        page_size: PAGE_SIZE,
        sort: sortState.sort as NonNullable<ArchiveEntitiesQuery['sort']>,
        order: sortState.order,
        ...(search ? { search } : {}),
      }),
  })

  const detail = useQuery({
    queryKey: ['archive-entity', kind, openId],
    queryFn: () => api.get<ArchiveEntityDetail>(`/api/archive/entities/${kind}/${openId}`),
    enabled: openId !== null,
  })

  const reindex = useMutation({
    mutationFn: (dryRun: boolean) =>
      api.post<ReindexResponse>('/api/archive/reindex', { dry_run: dryRun }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['archive-stats'] })
      void queryClient.invalidateQueries({ queryKey: ['archive-entities'] })
    },
  })

  // Sorting resets to page 1: staying on page 7 of a differently-ordered set shows rows
  // the operator did not navigate to, and an empty table when the set shrank.
  function applySort(next: SortState) {
    setSortState(next)
    setPage(1)
  }

  const data = stats.data

  return (
    <>
      <div className="panel">
        <h2>
          Archive capacity{' '}
          {data && <span className={`badge ${stateBadge[data.state]}`}>{data.state}</span>}
        </h2>
        {stats.isError && <ErrorNote error={stats.error} />}
        {data && (
          <>
            <p>
              {bytes(data.payload_bytes)} of {bytes(data.cap_bytes)} stored Last.fm payload (
              {Math.min(100, Math.round(data.used_ratio * 100))}%),{' '}
              {bytes(data.headroom_bytes)} headroom.
            </p>
            <div className="meter">
              <div
                className={`meter-fill ${data.state}`}
                style={{ width: `${Math.min(100, data.used_ratio * 100)}%` }}
              />
            </div>
            <p className="muted small">
              The Last.fm Terms of Service cap stored Last.fm Data at 100 MB. Bytes are
              measured and shown; nothing is ever deleted automatically — reaching the cap
              refuses new writes instead.
            </p>
            <div className="stat-grid">
              <Stat label="Distinct response bodies" value={data.response_rows} />
              <Stat label="Observations logged" value={data.observations} />
              <Stat label="Requests recorded" value={data.request_rows} />
              <Stat label="Stray archive reads" value={data.stray_archive_reads} />
              <Stat
                label="Cache hit ratio"
                value={
                  data.reads.hit_ratio === null
                    ? '—'
                    : `${Math.round(data.reads.hit_ratio * 100)}%`
                }
              />
              <Stat label="Oldest observation" value={data.oldest_request_at ?? '—'} mono />
            </div>
            <p className="muted small">
              {data.reads.note} Identical response bodies are stored once while every
              request against them is still logged, which is why observations normally
              exceed distinct bodies.
            </p>
          </>
        )}
      </div>

      {data && (
        <div className="panel">
          <h3>Derived layer</h3>
          <div className="stat-grid">
            {Object.entries(data.derived.counts).map(([table, count]) => (
              <Stat key={table} label={table.replace('lastfm_', '')} value={count} />
            ))}
          </div>
          <div className="actions">
            <button
              className="pill"
              onClick={() => reindex.mutate(true)}
              disabled={reindex.isPending}
            >
              Dry-run reindex
            </button>
            <button
              className="pill"
              onClick={() => reindex.mutate(false)}
              disabled={reindex.isPending}
            >
              Rebuild derived layer
            </button>
            <span className="muted small">
              The derived layer is a pure function of the raw archive, so a rebuild is
              reproducible and cannot lose data.
            </span>
          </div>
          {reindex.data && (
            <p className="small">
              <span className={reindex.data.dry_run ? 'badge' : 'badge ok'}>
                {reindex.data.dry_run ? 'dry run' : 'written'}
              </span>{' '}
              {Object.entries(reindex.data.counts)
                .map(([key, value]) => `${key} ${value}`)
                .join(' · ')}
              {reindex.data.counts.unexpected_shapes > 0 ? (
                <span className="badge danger">
                  {' '}
                  {reindex.data.counts.unexpected_shapes} unreadable shapes
                </span>
              ) : (
                <span className="badge ok"> no unreadable shapes</span>
              )}
            </p>
          )}
          {reindex.isError && <ErrorNote error={reindex.error} />}
        </div>
      )}

      <div className="panel">
        <h3>Stored entities</h3>
        <div className="row">
          <div className="pill-row">
            {KINDS.map((entry) => (
              <button
                key={entry}
                className={entry === kind ? 'pill active' : 'pill'}
                onClick={() => {
                  setKind(entry)
                  setOpenId(null)
                  setPage(1)
                }}
              >
                {entry}s
              </button>
            ))}
          </div>
          <form
            className="search"
            onSubmit={(event) => {
              event.preventDefault()
              setSearch(draft)
              setPage(1)
            }}
          >
            <input
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              placeholder="Filter by name…"
              aria-label="Filter entities by name"
            />
            <button type="submit">Filter</button>
          </form>
        </div>
        {entities.isError && <ErrorNote error={entities.error} />}
        {entities.data && (
          <>
            <p className="muted small">
              {entities.data.total} stored {kind}s
            </p>
            <div className="table-wrap">
            <table className="grid">
              <thead>
                <tr>
                  <SortHeader
                    column="name"
                    label="Name"
                    current={sortState}
                    onSort={applySort}
                  />
                  <th>MBID</th>
                  <th>Tags</th>
                  <SortHeader
                    column="listeners"
                    label="Listeners"
                    current={sortState}
                    onSort={applySort}
                  />
                  <SortHeader
                    column="last_seen"
                    label="Last seen"
                    current={sortState}
                    onSort={applySort}
                  />
                </tr>
              </thead>
              <tbody>
                {entities.data.items.map((entity: ArchiveEntitySummary) => (
                  <tr key={entity.id}>
                    <td>
                      <button className="link" onClick={() => setOpenId(entity.id)}>
                        {entity.name}
                      </button>
                      {entity.url && (
                        <>
                          {' '}
                          <a href={entity.url} target="_blank" rel="noreferrer">
                            ↗
                          </a>
                        </>
                      )}
                    </td>
                    <td className="mono small">{entity.mbid?.slice(0, 8) ?? '—'}</td>
                    <td className="chips">
                      {entity.tags.slice(0, 4).map((tag) => (
                        <span className="chip" key={tag}>
                          {tag}
                        </span>
                      ))}
                    </td>
                    <td className="muted small">{entity.listeners?.toLocaleString() ?? '—'}</td>
                    <td className="muted small">
                      {entity.last_seen_at
                        ? new Date(entity.last_seen_at).toLocaleDateString()
                        : '—'}
                    </td>
                  </tr>
                ))}
                {entities.data.items.length === 0 && (
                  <tr>
                    <td colSpan={5} className="muted">
                      {search
                        ? 'No stored entity matches that name.'
                        : 'Nothing stored yet. The archive fills as Last.fm is queried.'}
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
            </div>
            <Pagination
              startIndex={(page - 1) * PAGE_SIZE}
              pageSize={PAGE_SIZE}
              returned={entities.data.items.length}
              total={entities.data.total}
              unit="stored entities"
              onStartIndex={(next) => setPage(Math.floor(next / PAGE_SIZE) + 1)}
            />
          </>
        )}
      </div>

      {detail.data && (
        <div className="panel">
          <h3>
            {detail.data.name}{' '}
            <button className="pill" onClick={() => setOpenId(null)}>
              close
            </button>
          </h3>
          <p className="muted small">
            {[
              detail.data.identity,
              detail.data.mbid ? `mbid ${detail.data.mbid}` : null,
              detail.data.listeners !== null ? `${detail.data.listeners} listeners` : null,
              detail.data.playcount !== null ? `${detail.data.playcount} plays` : null,
            ]
              .filter(Boolean)
              .join(' · ')}
          </p>
          {detail.data.overview && <p className="small">{detail.data.overview}</p>}

          {detail.data.tags.length > 0 && (
            <>
              <h4>Tags by popularity</h4>
              <table className="grid">
                <thead>
                  <tr>
                    <th>#</th>
                    <th>Tag</th>
                    <th>Count</th>
                  </tr>
                </thead>
                <tbody>
                  {detail.data.tags.map((tag) => (
                    <tr key={tag.name}>
                      <td className="muted">{tag.rank}</td>
                      <td>{tag.name}</td>
                      <td className="mono">
                        {tag.count ?? <span className="muted">—</span>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          )}

          {detail.data.similar.length > 0 && (
            <>
              <h4>Similar artists</h4>
              <div className="chips">
                {detail.data.similar.map((peer) => (
                  <span className="chip" key={peer.name}>
                    {peer.name}
                    {peer.match !== null && (
                      <span className="muted small"> {peer.match.toFixed(2)}</span>
                    )}
                  </span>
                ))}
              </div>
            </>
          )}
        </div>
      )}
    </>
  )
}

function Stat({
  label,
  value,
  mono,
}: {
  label: string
  value: string | number
  mono?: boolean
}) {
  return (
    <div>
      <div className="muted small">{label}</div>
      <div className={mono ? 'mono small' : undefined}>
        {typeof value === 'number' ? value.toLocaleString() : value}
      </div>
    </div>
  )
}
