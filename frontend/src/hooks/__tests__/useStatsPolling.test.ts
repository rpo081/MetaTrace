import { act, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiError } from '../../api'
import { AuthProvider, useAuth } from '../../features/auth/AuthContext'
import { getAccessToken, setAccessToken } from '../../lib/authStorage'
import { useStatsPolling } from '../useStatsPolling'

// Mock the api module — the hook calls getStats / getRescanDelta on every
// tick; we control success/failure per-call via vi.fn().mockImplementationOnce.
const getStats = vi.fn()
const getRescanDelta = vi.fn()

vi.mock('../../api', async () => {
  const actual = await vi.importActual<typeof import('../../api')>('../../api')
  return {
    ...actual,
    getStats: (...args: unknown[]) => getStats(...(args as [])),
    getRescanDelta: (...args: unknown[]) => getRescanDelta(...(args as [])),
  }
})

// Mock fetch so AuthProvider boot + the silent refresh in withAuthRetry both
// behave predictably. The hook itself uses the mocked getStats, but withAuthRetry
// reaches AuthContext.refresh() → refreshAccessToken → fetch('/api/auth/refresh').
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
  getStats.mockReset()
  getRescanDelta.mockReset()
  // Default: both calls succeed with empty stats/delta so the hook settles.
  getStats.mockResolvedValue({
    state: 'idle',
    counts: { images: 0, indexed: 0, scanned: 0 },
    last_report: null,
  })
  getRescanDelta.mockResolvedValue({
    added: [],
    modified: [],
    removed: [],
    total_changes: 0,
  })
  fetchMock = vi.fn()
  globalThis.fetch = fetchMock as unknown as typeof fetch
})

afterEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
  setAccessToken(null)
})

describe('useStatsPolling', () => {
  it('boot_with_valid_token_marks_statsErrorKind_null', async () => {
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

    const { result } = renderHook(() => useStatsPolling(), { wrapper: AuthProvider })

    await waitFor(() => {
      expect(result.current.stats).not.toBeNull()
    })

    expect(result.current.statsErrorKind).toBeNull()
    expect(getStats).toHaveBeenCalled()
  })

  it('transient_401_triggers_withAuthRetry_and_resolves', async () => {
    localStorage.setItem(STORAGE_KEY, 'expired-token')

    // First getStats throws ApiError(401), second succeeds. The hook should
    // bounce through withAuthRetry (which calls /api/auth/refresh) and
    // eventually settle without setting statsErrorKind.
    let statsCalls = 0
    getStats.mockImplementation(async () => {
      statsCalls += 1
      if (statsCalls === 1) {
        throw new ApiError(401, 'session expired')
      }
      return {
        state: 'idle',
        counts: { images: 0, indexed: 0, scanned: 0 },
        last_report: null,
      }
    })

    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input.toString()
      if (url.endsWith('/api/auth/me')) {
        return mockJsonResponse({
          user: {
            id: 2, username: 'bob', email: 'b@e.com', role: 'viewer',
            is_active: true, created_at: '2026-01-01T00:00:00Z', last_login: null,
          },
          must_change_password: false,
        })
      }
      if (url.endsWith('/api/auth/refresh')) {
        return mockJsonResponse({ access_token: 'rotated-token' })
      }
      throw new Error(`unexpected fetch ${url}`)
    })

    const { result } = renderHook(() => useStatsPolling(), { wrapper: AuthProvider })

    // Wait until the token was rotated (proves the hook's first call hit
    // ApiError(401), withAuthRetry called /api/auth/refresh, and the retry
    // succeeded).
    await waitFor(() => {
      expect(getAccessToken()).toBe('rotated-token')
    })

    // The hook resolved stats successfully, so no error banner.
    await waitFor(() => {
      expect(result.current.stats).not.toBeNull()
    })
    expect(result.current.statsErrorKind).toBeNull()
    expect(statsCalls).toBeGreaterThanOrEqual(2)
  })

  it('auth_error_keeps_session_expired_banner_when_refresh_also_fails', async () => {
    localStorage.setItem(STORAGE_KEY, 'expired-token')

    // Always 401 from getStats; refresh cookie also gone → ApiError(401) escapes
    // withAuthRetry, hook sets statsErrorKind='auth'.
    getStats.mockImplementation(async () => {
      throw new ApiError(401, 'session expired')
    })

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
        // No refresh cookie: refresh fails with 401 too.
        return mockJsonResponse({ detail: 'no cookie' }, { status: 401, ok: false })
      }
      throw new Error(`unexpected fetch ${url}`)
    })

    const { result } = renderHook(() => useStatsPolling(), { wrapper: AuthProvider })

    await waitFor(() => {
      expect(result.current.statsErrorKind).toBe('auth')
    })
    expect(result.current.stats).toBeNull()
  })

  it('non_401_error_marks_statsErrorKind_network', async () => {
    localStorage.setItem(STORAGE_KEY, 'good-token')
    fetchMock.mockResolvedValue(
      mockJsonResponse({
        user: {
          id: 4, username: 'd', email: 'd@e.com', role: 'viewer',
          is_active: true, created_at: '2026-01-01T00:00:00Z', last_login: null,
        },
        must_change_password: false,
      }),
    )

    getStats.mockRejectedValueOnce(new Error('NetworkError: ECONNREFUSED'))

    const { result } = renderHook(() => useStatsPolling(), { wrapper: AuthProvider })

    await waitFor(() => {
      expect(result.current.statsErrorKind).toBe('network')
    })
  })

  it('api_error_500_marks_statsErrorKind_server', async () => {
    localStorage.setItem(STORAGE_KEY, 'good-token')
    fetchMock.mockResolvedValue(
      mockJsonResponse({
        user: {
          id: 5, username: 'e', email: 'e@e.com', role: 'viewer',
          is_active: true, created_at: '2026-01-01T00:00:00Z', last_login: null,
        },
        must_change_password: false,
      }),
    )

    getStats.mockRejectedValueOnce(new ApiError(500, 'boom'))

    const { result } = renderHook(() => useStatsPolling(), { wrapper: AuthProvider })

    await waitFor(() => {
      expect(result.current.statsErrorKind).toBe('server')
    })
  })

  it('rate_limited_429_marks_statsErrorKind_server_not_network', async () => {
    localStorage.setItem(STORAGE_KEY, 'good-token')
    fetchMock.mockResolvedValue(
      mockJsonResponse({
        user: {
          id: 6, username: 'f', email: 'f@e.com', role: 'viewer',
          is_active: true, created_at: '2026-01-01T00:00:00Z', last_login: null,
        },
        must_change_password: false,
      }),
    )

    getStats.mockRejectedValueOnce(new ApiError(429, 'too many'))

    const { result } = renderHook(() => useStatsPolling(), { wrapper: AuthProvider })

    await waitFor(() => {
      expect(result.current.statsErrorKind).toBe('server')
    })
  })

  it('successful_poll_clears_previous_error_kind', async () => {
    localStorage.setItem(STORAGE_KEY, 'good-token')
    fetchMock.mockResolvedValue(
      mockJsonResponse({
        user: {
          id: 5, username: 'e', email: 'e@e.com', role: 'viewer',
          is_active: true, created_at: '2026-01-01T00:00:00Z', last_login: null,
        },
        must_change_password: false,
      }),
    )

    getStats
      .mockRejectedValueOnce(new Error('boom'))
      .mockResolvedValueOnce({
        state: 'idle',
        counts: { images: 0, indexed: 0, scanned: 0 },
        last_report: null,
      })

    const { result } = renderHook(() => useStatsPolling(), { wrapper: AuthProvider })

    await waitFor(() => {
      expect(result.current.statsErrorKind).toBe('network')
    })

    await act(async () => {
      result.current.refreshStats()
    })

    await waitFor(() => {
      expect(result.current.statsErrorKind).toBeNull()
    })
  })
})