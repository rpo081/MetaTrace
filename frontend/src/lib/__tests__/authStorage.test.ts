import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  authHeaders,
  clearAllTokens,
  fetchWithAuth,
  getAccessToken,
  getAdminToken,
  setAccessToken,
  setPendingRefresh,
  authenticatedUrl,
} from '../authStorage'
import { refreshAccessToken } from '../../features/auth/api'

function jsonStream(body: unknown, init: { status?: number; ok?: boolean } = {}) {
  return {
    ok: init.ok ?? (init.status == null || (init.status >= 200 && init.status < 300)),
    status: init.status ?? 200,
    json: async () => body,
  } as unknown as Response
}

beforeEach(() => {
  clearAllTokens()
  setPendingRefresh(null)
})
afterEach(() => {
  vi.restoreAllMocks()
  clearAllTokens()
  setPendingRefresh(null)
})

describe('authStorage', () => {
  it('getAccessToken returns null when empty', () => {
    expect(getAccessToken()).toBeNull()
    expect(getAdminToken()).toBeNull()
  })
  it('setAccessToken writes and clears', () => {
    setAccessToken('tok123')
    expect(getAccessToken()).toBe('tok123')
    setAccessToken(null)
    expect(getAccessToken()).toBeNull()
  })
  it('authHeaders prefers Bearer', () => {
    localStorage.setItem('metatrace_access_token', 'bear')
    localStorage.setItem('metatrace_admin_token', 'legacy')
    expect(authHeaders()).toEqual({ Authorization: 'Bearer bear' })
  })
  it('authHeaders falls back to X-Admin-Token', () => {
    localStorage.setItem('metatrace_admin_token', 'legacy')
    expect(authHeaders()).toEqual({ 'X-Admin-Token': 'legacy' })
  })
  it('authHeaders empty when no token', () => {
    expect(authHeaders()).toEqual({})
  })
  it('authenticatedUrl appends token', () => {
    localStorage.setItem('metatrace_access_token', 'a b&c')
    const url = authenticatedUrl('/api/thumb/1?size=256')
    expect(url).toBe('/api/thumb/1?size=256&token=a%20b%26c')
  })
  it('authenticatedUrl without token returns plain', () => {
    expect(authenticatedUrl('/api/thumb/1')).toBe('/api/thumb/1')
  })
})

// ---------------------------------------------------------------------------
// fetchWithAuth — central 401 → silent refresh → single retry
// ---------------------------------------------------------------------------

describe('fetchWithAuth', () => {
  const unauthorized = jsonStream({ detail: 'expired token' }, { status: 401, ok: false })
  const ok = jsonStream({ ok: true })

  it('returns the 401 response untouched when no refresh is registered', async () => {
    setAccessToken('old-token')
    const fetchMock = vi.fn().mockResolvedValue(unauthorized)
    globalThis.fetch = fetchMock as unknown as typeof fetch

    const res = await fetchWithAuth('/api/stats')
    expect(res).toBe(unauthorized)
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('waits for the pending refresh and retries once with the rotated token', async () => {
    setAccessToken('old-token')
    const fetchMock = vi.fn()
    fetchMock
      .mockResolvedValueOnce(unauthorized)
      .mockResolvedValueOnce(ok)
    globalThis.fetch = fetchMock as unknown as typeof fetch

    setPendingRefresh(Promise.resolve('new-token'))
    const res = await fetchWithAuth('/api/stats')

    expect(fetchMock).toHaveBeenCalledTimes(2)
    expect(res).toBe(ok)
    const firstHeaders = new Headers((fetchMock.mock.calls[0][1] as RequestInit | undefined)?.headers)
    expect(firstHeaders.get('Authorization')).toBe('Bearer old-token')
    const retryHeaders = new Headers((fetchMock.mock.calls[1][1] as RequestInit | undefined)?.headers)
    expect(retryHeaders.get('Authorization')).toBe('Bearer new-token')
    // credentials must stay 'include' on the retry (httpOnly cookie fallback)
    expect((fetchMock.mock.calls[1][1] as RequestInit).credentials).toBe('include')
  })

  it('returns the 401 response without retrying when the refresh resolves null', async () => {
    setAccessToken('old-token')
    const fetchMock = vi.fn().mockResolvedValue(unauthorized)
    globalThis.fetch = fetchMock as unknown as typeof fetch

    setPendingRefresh(Promise.resolve(null))
    const res = await fetchWithAuth('/api/stats')
    expect(res).toBe(unauthorized)
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('passes non-401 responses through without consulting the refresh handler', async () => {
    setAccessToken('old-token')
    const fetchMock = vi.fn().mockResolvedValue(ok)
    globalThis.fetch = fetchMock as unknown as typeof fetch

    setPendingRefresh(Promise.resolve('new-token'))
    const res = await fetchWithAuth('/api/stats')
    expect(res).toBe(ok)
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('falls back to X-Admin-Token when no access token is set', async () => {
    localStorage.setItem('metatrace_admin_token', 'adm-secret')
    const fetchMock = vi.fn().mockResolvedValue(ok)
    globalThis.fetch = fetchMock as unknown as typeof fetch

    await fetchWithAuth('/api/things')
    const headers = new Headers((fetchMock.mock.calls[0][1] as RequestInit).headers)
    expect(headers.get('X-Admin-Token')).toBe('adm-secret')
  })
})

// ---------------------------------------------------------------------------
// refreshAccessToken — status → token/null/throw boundary
// ---------------------------------------------------------------------------

describe('refreshAccessToken', () => {
  afterEach(() => {
    setAccessToken(null)
  })

  it('returns the token and stores it on 200', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(
      jsonStream({ access_token: 'rotated-abc', token_type: 'bearer' }),
    ) as unknown as typeof fetch

    const token = await refreshAccessToken()
    expect(token).toBe('rotated-abc')
    expect(getAccessToken()).toBe('rotated-abc')
  })

  it('returns null on 401 (session definitively dead)', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(
      jsonStream({ detail: 'invalid or expired session' }, { status: 401, ok: false }),
    ) as unknown as typeof fetch

    const token = await refreshAccessToken()
    expect(token).toBeNull()
    expect(getAccessToken()).toBeNull()
  })

  it('returns null on 403', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(
      jsonStream({ detail: 'forbidden' }, { status: 403, ok: false }),
    ) as unknown as typeof fetch

    expect(await refreshAccessToken()).toBeNull()
  })

  it('throws ApiError(429) on rate-limit', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(
      jsonStream({ detail: 'rate limited' }, { status: 429, ok: false }),
    ) as unknown as typeof fetch

    await expect(refreshAccessToken()).rejects.toThrow(expect.objectContaining({ status: 429 }))
  })

  it('throws ApiError(500) on server error', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(
      jsonStream({ detail: 'boom' }, { status: 500, ok: false }),
    ) as unknown as typeof fetch

    await expect(refreshAccessToken()).rejects.toThrow(expect.objectContaining({ status: 500 }))
    // Transient failure must NOT clear the existing token.
    expect(getAccessToken()).toBeNull()
  })

  it('returns null when a 200 response carries no access_token', async () => {
    setAccessToken('stale-token')
    globalThis.fetch = vi.fn().mockResolvedValue(jsonStream({ ok: true })) as unknown as typeof fetch

    const token = await refreshAccessToken()
    expect(token).toBeNull()
    // No access_token present → setAccessToken must NOT be called.
    expect(getAccessToken()).toBe('stale-token')
  })
})
