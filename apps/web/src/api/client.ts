/**
 * The single HTTP boundary of the SPA.
 *
 * The browser only ever talks to our own /api origin: the Jellyfin admin key
 * and the Last.fm key stay server-side and are never exposed here.
 */

export interface ApiErrorBody {
  error?: { code?: string; message?: string }
}

export class ApiError extends Error {
  readonly status: number
  readonly code: string

  constructor(status: number, code: string, message: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...(init?.headers ?? {}),
    },
  })

  if (!response.ok) {
    let code = `http_${response.status}`
    let message = response.statusText || 'Request failed'
    try {
      const body = (await response.json()) as ApiErrorBody
      if (body.error?.code) code = body.error.code
      if (body.error?.message) message = body.error.message
    } catch {
      // Non-JSON error body; the status-based defaults stand.
    }
    throw new ApiError(response.status, code, message)
  }

  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}

export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body?: unknown) => {
    // exactOptionalPropertyTypes: build the init object rather than passing an
    // explicitly-undefined `body`, which RequestInit rejects.
    const init: RequestInit = { method: 'POST' }
    if (body !== undefined) init.body = JSON.stringify(body)
    return request<T>(path, init)
  },
}
