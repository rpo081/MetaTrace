import { act, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiError } from '../../../api'
import { AuthProvider, useAuth } from '../AuthContext'
import { getAccessToken, setAccessToken } from '../../../lib/authStorage'

// Stub storage between tests so access-token state doesn't leak (memory + localStorage).
const STORAGE_KEY = 'metatrace_access_token'

function mockJsonResponse(body: unknown, init: { status?: number; ok?: boolean } = {}) {
  return {
    ok: init.ok ?? (init.status == null || (init.status >= 200 && init.status < 300)),
    status: init.status ?? 200,
    json: async () => body,
  } as unknown as Response
}

let fetchMock: ReturnType<typeof vi.fn>

beforeEach(() => {
  localStorage.clear()
  setAccessToken(null)
  fetchMock = vi.fn()
  globalThis.fetch = fetchMock as unknown as typeof fetch
})

afterEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
  setAccessToken(null)
})

describe('AuthContext', () => {
  it('boot_with_no_token_sets_unauthenticated', async () => {
    // Memory-only token: boot tries silent refresh via httpOnly cookie, then falls back to unauthenticated.
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input.toString()
      if (url.endsWith('/api/auth/refresh')) {
        return mockJsonResponse({ detail: 'no refresh cookie' }, { status: 401, ok: false })
      }
      return mockJsonResponse({ detail: 'should not be called' }, { status: 401, ok: false })
    })

    const { result } = renderHook(() => useAuth(), { wrapper: AuthProvider })

    await waitFor(() => {
      expect(result.current.state.status).toBe('unauthenticated')
    })

    // Boot tried silent refresh, not /me.
    expect(fetchMock).toHaveBeenCalledWith('/api/auth/refresh', expect.anything())
    expect(result.current.state.user).toBeNull()
    expect(result.current.state.mustChangePassword).toBe(false)
  })

  it('boot_with_valid_token_sets_authenticated', async () => {
    localStorage.setItem(STORAGE_KEY, 'good-token')
    fetchMock.mockResolvedValue(
      mockJsonResponse({
        user: {
          id: 1, username: 'alice', email: 'a@e.com', role: 'viewer',
          is_active: true, created_at: '2026-01-01T00:00:00Z', last_login: null,
        },
        must_change_password: false,
      }),
    )

    const { result } = renderHook(() => useAuth(), { wrapper: AuthProvider })

    await waitFor(() => {
      expect(result.current.state.status).toBe('authenticated')
    })

    expect(result.current.state.user?.username).toBe('alice')
    expect(result.current.state.mustChangePassword).toBe(false)
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/auth/me',
      expect.objectContaining({ headers: expect.any(Object) }),
    )
  })

  it('login_writes_token_and_calls_me', async () => {
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input.toString()
      if (url.endsWith('/api/auth/login')) {
        return mockJsonResponse({
          access_token: 'fresh-token',
          user: {
            id: 2, username: 'bob', email: 'b@e.com', role: 'editor',
            is_active: true, created_at: '2026-01-01T00:00:00Z', last_login: null,
          },
        })
      }
      if (url.endsWith('/api/auth/me')) {
        return mockJsonResponse({
          user: {
            id: 2, username: 'bob', email: 'b@e.com', role: 'editor',
            is_active: true, created_at: '2026-01-01T00:00:00Z', last_login: null,
          },
          must_change_password: true,
        })
      }
      throw new Error(`unexpected fetch ${url}`)
    })

    const { result } = renderHook(() => useAuth(), { wrapper: AuthProvider })

    await waitFor(() => {
      expect(result.current.state.status).toBe('unauthenticated')
    })

    await act(async () => {
      await result.current.login('bob', 'Good-Password-123')
    })

    expect(getAccessToken()).toBe('fresh-token')
    expect(localStorage.getItem(STORAGE_KEY)).toBeNull() // memory-only, not persisted
    expect(result.current.state.status).toBe('authenticated')
    expect(result.current.state.user?.username).toBe('bob')
    expect(result.current.state.mustChangePassword).toBe(true)
  })

  it('withAuthRetry_refreshes_on_401', async () => {
    localStorage.setItem(STORAGE_KEY, 'expired-token')

    let statsCalls = 0
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input.toString()
      if (url.endsWith('/api/auth/me')) {
        return mockJsonResponse({
          user: {
            id: 3, username: 'c', email: 'c@e.com', role: 'viewer',
            is_active: true, created_at: '2026-01-01T00:00:00Z', last_login: null,
          },
          must_change_password: false,
        })
      }
      if (url.endsWith('/api/auth/refresh')) {
        return mockJsonResponse({ access_token: 'rotated-token' })
      }
      if (url.endsWith('/api/stats')) {
        statsCalls += 1
        if (statsCalls === 1) {
          return mockJsonResponse({ detail: 'expired' }, { status: 401, ok: false })
        }
        return mockJsonResponse({ ok: true })
      }
      throw new Error(`unexpected fetch ${url}`)
    })

    const { result } = renderHook(() => useAuth(), { wrapper: AuthProvider })
    await waitFor(() => {
      expect(result.current.state.status).toBe('authenticated')
    })

    await act(async () => {
      await result.current.withAuthRetry(async () => {
        const res = await fetch('/api/stats')
        if (!res.ok) {
          throw new ApiError(res.status, 'expired')
        }
        return res
      })
    })

    expect(statsCalls).toBeGreaterThanOrEqual(2)
    expect(getAccessToken()).toBe('rotated-token')
  })

  it('logout_clears_state_and_token', async () => {
    localStorage.setItem(STORAGE_KEY, 'token')
    fetchMock.mockResolvedValue(
      mockJsonResponse({
        user: {
          id: 4, username: 'd', email: 'd@e.com', role: 'viewer',
          is_active: true, created_at: '2026-01-01T00:00:00Z', last_login: null,
        },
        must_change_password: false,
      }),
    )

    const { result } = renderHook(() => useAuth(), { wrapper: AuthProvider })
    await waitFor(() => {
      expect(result.current.state.status).toBe('authenticated')
    })

    await act(async () => {
      await result.current.logout()
    })

    expect(getAccessToken()).toBeNull()
    expect(localStorage.getItem(STORAGE_KEY)).toBeNull()
    expect(result.current.state.status).toBe('unauthenticated')
    expect(result.current.state.user).toBeNull()
  })
})