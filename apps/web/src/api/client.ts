/**
 * The single HTTP boundary of the SPA.
 *
 * The browser only ever talks to our own /api origin: the Jellyfin admin key and the
 * Last.fm key stay server-side and are never exposed here.
 */

export interface ApiErrorBody {
  error?: { code?: string; message?: string; retryable?: boolean; current?: unknown }
}

export class ApiError extends Error {
  readonly status: number
  readonly code: string
  readonly retryable: boolean

  constructor(status: number, code: string, message: string, retryable = false) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.retryable = retryable
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
  })

  if (!response.ok) {
    let code = `http_${response.status}`
    let message = response.statusText || 'Request failed'
    let retryable = false
    try {
      const body = (await response.json()) as ApiErrorBody
      if (body.error?.code) code = body.error.code
      if (body.error?.message) message = body.error.message
      retryable = Boolean(body.error?.retryable)
    } catch {
      // Non-JSON error body; the status-based defaults stand.
    }
    throw new ApiError(response.status, code, message, retryable)
  }

  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}

/**
 * Read a Server-Sent Events response.
 *
 * The bulk endpoints stream rather than returning one payload, because a run over
 * hundreds of items is long and partly irreversible: progress has to be visible per
 * item, so a failure is reported where it happened rather than at the end.
 */
async function stream<T>(
  path: string,
  body: unknown,
  onEvent: (event: T) => void,
  signal?: AbortSignal,
): Promise<void> {
  const response = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    ...(signal ? { signal } : {}),
  })

  if (!response.ok) {
    let code = `http_${response.status}`
    let message = response.statusText || 'Request failed'
    try {
      const payload = (await response.json()) as ApiErrorBody
      if (payload.error?.code) code = payload.error.code
      if (payload.error?.message) message = payload.error.message
    } catch {
      // keep the defaults
    }
    throw new ApiError(response.status, code, message)
  }
  if (!response.body) throw new ApiError(500, 'no_stream', 'The response had no body')

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  // SSE frames are separated by a blank line, and a frame can span reads.
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    const frames = buffer.split('\n\n')
    buffer = frames.pop() ?? ''
    for (const frame of frames) {
      for (const line of frame.split('\n')) {
        if (!line.startsWith('data: ')) continue
        try {
          onEvent(JSON.parse(line.slice(6)) as T)
        } catch {
          // A malformed frame is not worth aborting a running batch over.
        }
      }
    }
  }
}

// ---------------------------------------------------------------------- types

export interface LibraryInfo {
  id: string | null
  name: string
  locations: string[]
}

export interface ItemSummary {
  id: string
  kind: 'artist' | 'album' | 'song'
  name: string
  album: string | null
  album_artist: string | null
  year: number | null
  genres: string[]
  tags: string[]
  has_provider_ids: boolean
  has_overview: boolean
  child_count: number | null
}

export interface ItemSummaryPage {
  items: ItemSummary[]
  total: number
  start_index: number
}

export interface InfoResponse {
  app_version: string
  jellyfin: {
    reachable: boolean
    server_name?: string | null
    version?: string | null
    key_configured: boolean
    elevated?: boolean | null
    error?: string | null
  }
  lastfm: { configured: boolean; api_root: string }
  archive: { enabled: boolean; log_requests: boolean; cap_bytes: number }
}

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
  stray_archive_reads: number
  reads: {
    hits: number
    misses: number
    decisions: number
    hit_ratio: number | null
    scope: string
    note: string
  }
  derived: { counts: Record<string, number>; total: number; newest_observation_at: string | null }
  partitions: { name: string; rows: number }[]
}

export interface ConfidenceComponent {
  name: string
  value: number | null
  applied_weight: number
  detail: string
}

export interface ConfidenceView {
  total: number
  verdict: 'auto' | 'review' | 'reject'
  accepts_by_default: boolean
  mbid_conflict: boolean
  components: ConfidenceComponent[]
  notes: string[]
}

export interface CandidateView {
  entity_kind: string
  entity_id: number
  identity: string
  name: string
  mbid: string | null
  artist: string | null
  year: number | null
  duration_ms: number | null
  url: string | null
  listeners: number | null
  playcount: number | null
  tags: { name: string; count: number | null }[]
  has_overview: boolean
  last_seen_at: string | null
  latest_response_id: string | null
  matched_on: 'mbid' | 'name'
  confidence: ConfidenceView
}

export interface CandidatesResponse {
  item_id: string
  kind: string
  name: string
  etag: string | null
  candidates: CandidateView[]
  count: number
  note: string
}

export interface FieldChangeView {
  field: string
  current: unknown
  proposed: unknown
  mode: string
  reason: string
  source: string
  selected: boolean
  changes_anything: boolean
  withheld_reason: string | null
  provenance: { response_id?: string | null; request_ids?: number[] }
}

export interface DiffResponse {
  item_id: string
  kind: string
  name: string
  etag: string | null
  locked_fields: string[]
  confidence: ConfidenceView
  candidate: {
    name: string
    mbid: string | null
    artist: string | null
    url: string | null
    year: number | null
    response_id: string | null
  }
  changes: FieldChangeView[]
  withheld: FieldChangeView[]
  default_selection: string[]
}

export interface ApplyResponse {
  item_id: string
  kind: string
  applied: string[]
  unchanged: string[]
  snapshot_id: number | null
  etag_before: string | null
  etag_after: string | null
  duration_ms: number
  state: Record<string, unknown> | null
}

export interface SnapshotSummary {
  id: number
  kind: string
  name: string | null
  source_op: string
  created_at: string | null
  etag: string | null
  field_count: number
}

export interface ArchiveEntitySummary {
  id: number
  identity: string
  kind: string
  name: string
  mbid: string | null
  url: string | null
  listeners: number | null
  playcount: number | null
  overview: string | null
  tags: string[]
  first_seen_at: string | null
  last_seen_at: string | null
  latest_response_id: string
  extra: Record<string, unknown>
}

export interface ArchiveEntityDetail extends ArchiveEntitySummary {
  tags_detail?: { name: string; rank: number; count: number | null }[]
  tags: string[]
  similar: { name: string; mbid: string | null; match: number | null; rank: number }[]
}

export type BulkEvent =
  | {
      type: 'item'
      index: number
      item_id: string
      name: string
      kind: string
      skipped_reason: string | null
      applicable: boolean
      diff: DiffResponse
    }
  | {
      type: 'applied'
      index: number
      item_id: string
      name: string
      applied_fields: string[]
      snapshot_id: number | null
    }
  | { type: 'failed'; index?: number; item_id: string; name?: string; error: string; error_code?: string }
  | { type: 'reverted'; item_id: string; restored_fields: string[] }
  | {
      type: 'summary'
      job_id?: string
      batch_id?: string
      items?: number
      applicable?: number
      skipped?: number
      applied?: number
      failed?: number
      reverted?: number
      failures?: { item_id: string; error: string }[]
      batch_revert?: string
    }
  | { type: 'error'; code: string; message: string }
  | { type: 'done' }

export interface BulkSelectionInput {
  kind: 'artist' | 'album' | 'song'
  ids?: string[]
  parent_id?: string | null
  search?: string | null
  limit?: number
  missing_metadata?: boolean
}

export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body?: unknown) => {
    const init: RequestInit = { method: 'POST' }
    if (body !== undefined) init.body = JSON.stringify(body)
    return request<T>(path, init)
  },
  stream,
}

export { stream as streamEvents }
