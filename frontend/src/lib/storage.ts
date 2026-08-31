/** Safe localStorage abstraction — wraps try/catch in one place.

 View-mode keys `metatrace_view_mode` and `metatrace_browse_view_mode`
 previously had 6× duplicated try/catch blocks. All view-mode persistence now
 routes through this module. Also centralises legacy admin-token cleanup.
*/

export const VIEW_MODE_KEY = 'metatrace_view_mode'
export const BROWSE_VIEW_MODE_KEY = 'metatrace_browse_view_mode'
export const LEGACY_ADMIN_TOKEN_KEY = 'metatrace_admin_token'
export const ACCESS_TOKEN_KEY = 'metatrace_access_token'

export type ViewMode = 'grid' | 'list'

export function safeGet(key: string): string | null {
  try {
    return localStorage.getItem(key)
  } catch {
    return null
  }
}

export function safeSet(key: string, value: string): void {
  try {
    localStorage.setItem(key, value)
  } catch {
    /* ignore — storage unavailable or quota exceeded */
  }
}

export function safeRemove(key: string): void {
  try {
    localStorage.removeItem(key)
  } catch {
    /* ignore */
  }
}

export function loadViewMode(key: string, fallback: ViewMode = 'grid'): ViewMode {
  const v = safeGet(key)
  if (v === 'grid' || v === 'list') return v
  return fallback
}

export function saveViewMode(key: string, mode: ViewMode): void {
  safeSet(key, mode)
}

/** Clear legacy X-Admin-Token from localStorage to avoid 403 vs 401 cascade.
 *  Old SPA versions persisted `metatrace_admin_token`; if left behind, requests
 *  send both Bearer and X-Admin-Token, and some proxies cache 403 differently
 *  from 401. Called on login and logout.
 */
export function clearLegacyAdminToken(): void {
  safeRemove(LEGACY_ADMIN_TOKEN_KEY)
  // Also clear access token legacy key (now memory-only)
  safeRemove(ACCESS_TOKEN_KEY)
}
