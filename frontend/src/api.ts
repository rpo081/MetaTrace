import type {
  SearchCombineMode,
  SearchResponse,
  Stats,
  RescanDeltaResponse,
  StoreSnapshotRunResult,
  StoreSnapshotSettings,
} from './types'

/** Error carrying the HTTP status so callers can special-case codes (e.g. 409). */
export class ApiError extends Error {
  status: number

  constructor(status: number, message: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

async function parseError(res: Response): Promise<never> {
  let detail = `${res.status} ${res.statusText}`
  try {
    const body = await res.json()
    if (body?.detail) detail = String(body.detail)
  } catch {
    /* keep status text */
  }
  throw new ApiError(res.status, detail)
}

export async function searchImage(
  file: File | null,
  k: number,
  minScore: number,
  q?: string,
  combine: SearchCombineMode = 'and',
  signal?: AbortSignal,
): Promise<SearchResponse> {
  const form = new FormData()
  if (file) {
    form.append('file', file)
  }
  const params = new URLSearchParams({
    k: String(k),
    min_score: String(minScore),
    combine,
  })
  if (q && q.trim()) {
    params.set('q', q.trim())
  }
  const res = await fetch(`/api/search?${params.toString()}`, {
    method: 'POST',
    body: form,
    signal,
  })
  if (!res.ok) await parseError(res)
  return res.json()
}

export async function getStats(signal?: AbortSignal): Promise<Stats> {
  const res = await fetch('/api/stats', { signal })
  if (!res.ok) await parseError(res)
  return res.json()
}

/** localStorage key holding the admin token sent as X-Admin-Token. */
const ADMIN_TOKEN_KEY = 'metatrace_admin_token'

function getAdminToken(): string | null {
  try {
    return localStorage.getItem(ADMIN_TOKEN_KEY)
  } catch {
    return null // storage unavailable (privacy mode etc.)
  }
}

export async function triggerRescan(rebuild = false, useDelta = true): Promise<void> {
  const headers: Record<string, string> = {}
  const token = getAdminToken()
  if (token) headers['X-Admin-Token'] = token
  const res = await fetch(
    `/api/rescan?rebuild=${rebuild}&use_delta=${useDelta}`,
    { method: 'POST', headers },
  )
  if (!res.ok) await parseError(res)
}

export async function pauseRescan(): Promise<void> {
  const headers: Record<string, string> = {}
  const token = getAdminToken()
  if (token) headers['X-Admin-Token'] = token
  const res = await fetch('/api/rescan/pause', { method: 'POST', headers })
  if (!res.ok) await parseError(res)
}

export async function resumeRescan(): Promise<void> {
  const headers: Record<string, string> = {}
  const token = getAdminToken()
  if (token) headers['X-Admin-Token'] = token
  const res = await fetch('/api/rescan/resume', { method: 'POST', headers })
  if (!res.ok) await parseError(res)
}

export async function getRescanDelta(signal?: AbortSignal): Promise<RescanDeltaResponse> {
  const res = await fetch('/api/rescan-delta', { signal })
  if (!res.ok) await parseError(res)
  return res.json()
}

export async function getStoreSnapshotSettings(signal?: AbortSignal): Promise<StoreSnapshotSettings> {
  const res = await fetch('/api/settings/store-snapshot', { signal })
  if (!res.ok) await parseError(res)
  return res.json()
}
export async function runStoreSnapshot(): Promise<StoreSnapshotRunResult> {
  const headers: Record<string, string> = {}
  const token = getAdminToken()
  if (token) headers['X-Admin-Token'] = token
  const res = await fetch('/api/settings/store-snapshot/run', {
    method: 'POST',
    headers,
  })
  if (!res.ok) await parseError(res)
  return res.json()
}
