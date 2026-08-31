import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from 'react'

import { ApiError } from '../../api'
import { setPendingRefresh } from '../../lib/authStorage'
import {
  changeOwnPassword as apiChangeOwnPassword,
  fetchMe,
  getAccessToken,
  login as apiLogin,
  logout as apiLogout,
  refreshAccessToken,
  setAccessToken,
  type MeResponse,
  type UserPublic,
} from './api'

// ---------------------------------------------------------------------------
// Public types
// ---------------------------------------------------------------------------

export type { Role, UserPublic } from './api'

export type AuthStatus = 'loading' | 'authenticated' | 'unauthenticated'

export interface AuthState {
  user: UserPublic | null
  status: AuthStatus
  mustChangePassword: boolean
}

export interface AuthApi {
  state: AuthState
  loadingTooLong: boolean
  login: (username: string, password: string) => Promise<void>
  logout: () => Promise<void>
  refresh: () => Promise<string | null>
  changePassword: (current: string, next: string) => Promise<void>
  retryBoot: () => void
  withAuthRetry: <T>(fn: () => Promise<T>) => Promise<T>
}

const INITIAL_STATE: AuthState = {
  user: null,
  status: 'loading',
  mustChangePassword: false,
}

// ---------------------------------------------------------------------------
// Provider
// ---------------------------------------------------------------------------

const AuthContext = createContext<AuthApi | null>(null)

const LOADING_TIMEOUT_MS = 5000

export function AuthProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<AuthState>(INITIAL_STATE)
  const [loadingTooLong, setLoadingTooLong] = useState(false)
  const [bootNonce, setBootNonce] = useState(0)

  // A single in-flight refresh promise — guards against stampeding refresh
  // requests when many API calls 401 in parallel.
  const inflightRefresh = useRef<Promise<string | null> | null>(null)

  // Boot sequence — runs once per (bootNonce, mount).
  // Memory-only token: on hard reload memory is empty but httpOnly refresh
  // cookie may still be valid. Try silent refresh before declaring unauthenticated.
  useEffect(() => {
    let cancelled = false
    const token = getAccessToken()
    const bootWithToken = (tok: string | null) => {
      if (!tok) {
        setState({ user: null, status: 'unauthenticated', mustChangePassword: false })
        return
      }
      fetchMe()
        .then((me: MeResponse) => {
          if (cancelled) return
          setState({
            user: me.user,
            status: 'authenticated',
            mustChangePassword: me.must_change_password,
          })
        })
        .catch((err: unknown) => {
          if (cancelled) return
          if (err instanceof ApiError && err.status === 401) {
            setAccessToken(null)
            setState({ user: null, status: 'unauthenticated', mustChangePassword: false })
            return
          }
        })
    }

    if (token) {
      bootWithToken(token)
    } else {
      // No memory token — try silent refresh via httpOnly cookie (7-day window).
      refreshAccessToken()
        .then((newToken) => {
          if (cancelled) return
          bootWithToken(newToken)
        })
        .catch(() => {
          if (cancelled) return
          setState({ user: null, status: 'unauthenticated', mustChangePassword: false })
        })
    }
    return () => {
      cancelled = true
    }
  }, [bootNonce])

  // Loading-state timeout — if /me is still in flight after LOADING_TIMEOUT_MS,
  // surface a Retry button on the loading screen.
  useEffect(() => {
    if (state.status !== 'loading') {
      setLoadingTooLong(false)
      return
    }
    const t = window.setTimeout(() => setLoadingTooLong(true), LOADING_TIMEOUT_MS)
    return () => window.clearTimeout(t)
  }, [state.status])

  const login = useCallback(async (username: string, password: string) => {
    const result = await apiLogin(username, password)
    setAccessToken(result.access_token)
    // Clear legacy admin token that would cause 403 vs 401 cascade
    try {
      const { clearLegacyAdminToken } = await import('../../lib/storage')
      clearLegacyAdminToken()
    } catch { /* ignore */ }
    // Fetch /me to pick up the must_change_password flag (plan-frontend §3.3).
    const me = await fetchMe()
    setState({
      user: me.user,
      status: 'authenticated',
      mustChangePassword: me.must_change_password,
    })
  }, [])

  const logout = useCallback(async () => {
    await apiLogout()
    setAccessToken(null)
    try {
      const { clearLegacyAdminToken } = await import('../../lib/storage')
      clearLegacyAdminToken()
    } catch { /* ignore */ }
    setState({ user: null, status: 'unauthenticated', mustChangePassword: false })
  }, [])

  const refresh = useCallback(async (): Promise<string | null> => {
    if (inflightRefresh.current) return inflightRefresh.current
    const p = (async () => {
      try {
        const t = await refreshAccessToken()
        if (t) setAccessToken(t)
        return t
      } finally {
        // No-op: cleanup happens in inflightRefresh.finally below.
      }
    })()
    inflightRefresh.current = p
    setPendingRefresh(p)
    p.finally(() => {
      inflightRefresh.current = null
      setPendingRefresh(null)
    })
    const token = await p
    if (!token) {
      setAccessToken(null)
      setState({ user: null, status: 'unauthenticated', mustChangePassword: false })
    }
    return token
  }, [])

  const changePassword = useCallback(async (current: string, next: string) => {
    await apiChangeOwnPassword(current, next)
    setState((prev) => ({ ...prev, mustChangePassword: false }))
  }, [])

  const retryBoot = useCallback(() => {
    setBootNonce((n) => n + 1)
  }, [])

  const withAuthRetry = useCallback(
    async <T,>(fn: () => Promise<T>): Promise<T> => {
      try {
        return await fn()
      } catch (err) {
        if (!(err instanceof ApiError) || err.status !== 401) throw err
        // Try a single refresh + retry. If refresh fails, fall through to
        // unauthenticated so <RequireAuth> can re-render the LoginPage.
        const token = await refresh()
        if (!token) throw err
        try {
          return await fn()
        } catch (err2) {
          // Even after retry, 401 means the user's session is dead — bubble
          // the error to the caller; RequireAuth will pick up the state flip
          // and re-render LoginPage.
          throw err2
        }
      }
    },
    [refresh],
  )

  const api: AuthApi = {
    state,
    loadingTooLong,
    login,
    logout,
    refresh,
    changePassword,
    retryBoot,
    withAuthRetry,
  }

  return <AuthContext.Provider value={api}>{children}</AuthContext.Provider>
}

// ---------------------------------------------------------------------------
// Hook
// ---------------------------------------------------------------------------

export function useAuth(): AuthApi {
  const ctx = useContext(AuthContext)
  if (!ctx) {
    throw new Error('useAuth must be used inside <AuthProvider>')
  }
  return ctx
}