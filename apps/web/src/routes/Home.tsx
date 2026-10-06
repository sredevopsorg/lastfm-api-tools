import { useQuery } from '@tanstack/react-query'
import { api } from '../api/client'

interface InfoResponse {
  jellyfin: { reachable: boolean; version?: string | null; elevated?: boolean | null; error?: string }
  lastfm: { configured: boolean }
  archive: { enabled: boolean; state?: string; payload_bytes?: number }
}

/**
 * Phase 0 shell: proves the container, the API mount and the SPA build are all
 * wired together. The library browser and metadata editor replace this view.
 */
export function Home() {
  const info = useQuery({
    queryKey: ['info'],
    queryFn: () => api.get<InfoResponse>('/api/info'),
    retry: false,
  })

  return (
    <div className="panel">
      <h2>Getting started</h2>
      {info.isLoading && <p className="muted">Contacting the API…</p>}
      {info.isError && (
        <>
          <p className="badge warn">API not ready</p>
          <p className="muted mono">{(info.error as Error).message}</p>
        </>
      )}
      {info.data && (
        <ul>
          <li>
            Jellyfin:{' '}
            {info.data.jellyfin.reachable ? (
              <span className="badge ok">{info.data.jellyfin.version ?? 'reachable'}</span>
            ) : (
              <span className="badge danger">{info.data.jellyfin.error ?? 'unreachable'}</span>
            )}
          </li>
          <li>
            Last.fm:{' '}
            {info.data.lastfm.configured ? (
              <span className="badge ok">key configured</span>
            ) : (
              <span className="badge warn">no API key</span>
            )}
          </li>
          <li>
            Archive:{' '}
            {info.data.archive.enabled ? (
              <span className="badge ok">{info.data.archive.state ?? 'enabled'}</span>
            ) : (
              <span className="badge warn">disabled</span>
            )}
          </li>
        </ul>
      )}
    </div>
  )
}
