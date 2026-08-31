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
