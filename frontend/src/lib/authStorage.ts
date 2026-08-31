/** Centralized auth storage — access token is memory-only + httpOnly cookie (XSS mitigation).
 *
 * The JWT lives in a module-scoped variable, not `localStorage`, so a
 * successful XSS cannot exfiltrate it via `localStorage.getItem`. On login/
 * refresh the server also sets an `httpOnly` `__Host-metatrace_access` cookie
 * (SameSite=Strict, Secure, Path=/) with 15m TTL as defense-in-depth — all
 * fetches use `credentials: 'include'` so the cookie authenticates even
 * without Authorization header. `httpOnly` refresh cookie (path
 * `/api/auth/refresh`) survives reloads; `AuthContext` boot tries a silent
 * `refreshAccessToken()` when no memory token exists. `localStorage` is kept
 * only as a one-time migration source for legacy installs and is cleared
 * after migration.
 */

export const ACCESS_TOKEN_KEY = 'metatrace_access_token'
export const ADMIN_TOKEN_KEY = 'metatrace_admin_token'

// Memory-only stores — never persisted to localStorage after migration.
let memoryAccessToken: string | null = null
let memoryAdminToken: string | null = null

function migrateLegacyTokens(): void {
  try {
    if (!memoryAccessToken) {
      const legacyAccess = localStorage.getItem(ACCESS_TOKEN_KEY)
      if (legacyAccess) {
        memoryAccessToken = legacyAccess
        localStorage.removeItem(ACCESS_TOKEN_KEY)
      }
    }
    if (!memoryAdminToken) {
      const legacyAdmin = localStorage.getItem(ADMIN_TOKEN_KEY)
      if (legacyAdmin) {
        memoryAdminToken = legacyAdmin
      }
    }
  } catch {
    /* ignore — storage unavailable */
  }
}

export function getAccessToken(): string | null {
  migrateLegacyTokens()
  return memoryAccessToken
}

export function setAccessToken(token: string | null): void {
  migrateLegacyTokens()
  memoryAccessToken = token
  // Explicitly clear legacy storage so token never stays in localStorage.
  try {
    localStorage.removeItem(ACCESS_TOKEN_KEY)
  } catch {
    /* ignore */
  }
}

export function getAdminToken(): string | null {
  migrateLegacyTokens()
  if (memoryAdminToken) return memoryAdminToken
  try {
    return localStorage.getItem(ADMIN_TOKEN_KEY)
  } catch {
    return null
  }
}

export function setAdminToken(token: string | null): void {
  memoryAdminToken = token
  try {
    if (token) localStorage.setItem(ADMIN_TOKEN_KEY, token)
    else localStorage.removeItem(ADMIN_TOKEN_KEY)
  } catch {
    /* ignore */
  }
}

export function clearAllTokens(): void {
  memoryAccessToken = null
  memoryAdminToken = null
  try {
    localStorage.removeItem(ACCESS_TOKEN_KEY)
    localStorage.removeItem(ADMIN_TOKEN_KEY)
  } catch {
    /* ignore */
  }
}

export function authHeaders(): Record<string, string> {
  const headers: Record<string, string> = {}
  const access = getAccessToken()
  if (access) {
    headers['Authorization'] = `Bearer ${access}`
  } else {
    const token = getAdminToken()
    if (token) headers['X-Admin-Token'] = token
  }
  return headers
}

/** @deprecated token-in-URL leaks to logs/history — prefer fetch+blob with authHeaders(). Kept for <img> fallback. */
export function authenticatedUrl(url: string): string {
  const token = getAccessToken() || getAdminToken()
  if (!token) return url
  const sep = url.includes('?') ? '&' : '?'
  return `${url}${sep}token=${encodeURIComponent(token)}`
}

// ---------------------------------------------------------------------------
// fetchWithAuth: central 401 → silent refresh → single retry.
//
// AuthProvider registers the in-flight refresh promise via setPendingRefresh()
// while a refresh is in flight (or right before issuing one). fetchWithAuth
// consults that slot on 401; if a refresh handler is present, it waits for
// the new token and retries the request exactly once with the fresh
// Authorization header. If no handler is registered, the 401 is returned
// untouched so callers can render the login screen.
// ---------------------------------------------------------------------------

let pendingRefresh: Promise<string | null> | null = null

export function setPendingRefresh(p: Promise<string | null> | null): void {
  pendingRefresh = p
}

function currentRefresh(): Promise<string | null> | null {
  return pendingRefresh
}

export async function fetchWithAuth(input: RequestInfo, init: RequestInit = {}): Promise<Response> {
  const headers = new Headers(init.headers || {})
  const access = getAccessToken()
  if (access) headers.set('Authorization', `Bearer ${access}`)
  else {
    const token = getAdminToken()
    if (token) headers.set('X-Admin-Token', token)
  }
  const first = await fetch(input, { ...init, headers, credentials: 'include' })
  if (first.status !== 401) return first
  const refreshP = currentRefresh()
  if (!refreshP) return first
  const newToken = await refreshP
  if (!newToken) return first
  const retryHeaders = new Headers(init.headers || {})
  retryHeaders.set('Authorization', `Bearer ${newToken}`)
  return fetch(input, { ...init, headers: retryHeaders, credentials: 'include' })
}
