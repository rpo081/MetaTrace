import type {
  BrowseFilters,
  BrowseResponse,
  BrowseSort,
  BrowseOrder,
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

/** Map HTTP status codes to user-friendly messages. */
const STATUS_MESSAGES: Record<number, string> = {
  400: 'Invalid request. Please check your input and try again.',
  401: 'Authentication required. Please check your admin token.',
  403: 'Access denied. You do not have permission for this action.',
  404: 'Resource not found.',
  409: 'A scan is already running. Please wait for it to finish.',
  413: 'The file is too large. Please use a smaller image.',
  422: 'Invalid parameters. Please check your input.',
  429: 'Too many requests. Please wait a moment and try again.',
  500: 'Server error. Please try again later.',
  502: 'Server is temporarily unavailable. Please try again later.',
  503: 'Service is currently unavailable. Please try again later.',
}

/** Return a user-friendly message for the HTTP status, falling back to the raw detail. */
function friendlyErrorMessage(status: number, detail: string): string {
  const mapped = STATUS_MESSAGES[status]
  if (mapped) return mapped
  if (status >= 500) return 'Server error. Please try again later.'
  return detail
}

async function parseError(res: Response): Promise<never> {
  let detail = `${res.status} ${res.statusText}`
  try {
    const body = await res.json()
    if (body?.detail) detail = String(body.detail)
  } catch {
    /* keep status text */
  }
  throw new ApiError(res.status, friendlyErrorMessage(res.status, detail))
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

export interface BrowseParams {
  offset?: number
  limit?: number
  sort?: BrowseSort
  order?: BrowseOrder
  filters?: BrowseFilters
}

export async function browseImages(
  params: BrowseParams,
  signal?: AbortSignal,
): Promise<BrowseResponse> {
  const sp = new URLSearchParams()
  const p: Record<string, string | number | boolean | undefined> = {
    offset: params.offset,
    limit: params.limit,
    sort: params.sort,
    order: params.order,
    ...params.filters,
  }
  for (const [k, v] of Object.entries(p)) {
    if (v !== undefined && v !== null && v !== '') {
      sp.set(k, String(v))
    }
  }
  const res = await fetch(`/api/images?${sp.toString()}`, { signal })
  if (!res.ok) await parseError(res)
  return res.json()
}
