import { ApiError } from '../../api'
import {
  authHeaders as buildAuthHeaders,
  getAccessToken,
  setAccessToken,
} from '../../lib/authStorage'
export { getAccessToken, setAccessToken } from '../../lib/authStorage'

export type Role = 'admin' | 'editor' | 'viewer'

export interface UserPublic {
  id: number
  username: string
  email: string
  role: Role
  is_active: boolean
  created_at: string
  last_login: string | null
}

export interface MeResponse {
  user: UserPublic
  must_change_password: boolean
}

export interface UserListItem {
  id: number
  username: string
  email: string
  role: Role
  is_active: boolean
  created_at: string
  last_login: string | null
}

export interface UserCreateRequest {
  username: string
  email?: string
  password: string
  role: Role
}

export interface UserUpdateRequest {
  email?: string
  role?: Role
  is_active?: boolean
}

// ---------------------------------------------------------------------------
// Auth
// ---------------------------------------------------------------------------

export async function login(
  username: string,
  password: string,
): Promise<{ access_token: string; user: UserPublic }> {
  const res = await fetch('/api/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify({ username, password }),
  })
  if (!res.ok) await throwApiError(res)
  return res.json()
}

export async function logout(): Promise<void> {
  // Best-effort: failures here must not block the local state clear in the
  // caller (refresh-token cookie may already be gone).
  try {
    await fetch('/api/auth/logout', {
      method: 'POST',
      headers: buildAuthHeaders(),
      credentials: 'include',
    })
  } catch {
    /* ignore network errors during logout */
  }
}

export async function refreshAccessToken(): Promise<string | null> {
  const res = await fetch('/api/auth/refresh', {
    method: 'POST',
    credentials: 'include',
  })
  if (res.ok) {
    const data = await res.json()
    if (data.access_token) setAccessToken(data.access_token)
    return data.access_token ?? null
  }
  // Definitive rejection (401/403): the refresh cookie is gone/revoked —
  // no session to restore. Callers treat `null` as "log out".
  if (res.status === 401 || res.status === 403) return null
  // Transient failure (429 rate-limit, 5xx server error): the session may
  // still be valid — throw instead of returning null so callers retry on the
  // same session rather than force-logging the user out.
  throw await throwApiError(res)
}

export async function fetchMe(): Promise<MeResponse> {
  const res = await fetch('/api/auth/me', { headers: buildAuthHeaders(), credentials: 'include' })
  if (!res.ok) await throwApiError(res)
  return res.json()
}

export async function getSignedFileUrl(imageId: number): Promise<string> {
  const res = await fetch(`/api/auth/file-token?image_id=${imageId}`, {
    headers: buildAuthHeaders(),
    credentials: 'include',
  })
  if (!res.ok) await throwApiError(res)
  const data = await res.json()
  return data.url as string
}

export async function changeOwnPassword(current: string, next: string): Promise<void> {
  const res = await fetch('/api/auth/change-password', {
    method: 'POST',
    headers: { ...buildAuthHeaders(), 'Content-Type': 'application/json' },
    body: JSON.stringify({ current_password: current, new_password: next }),
    credentials: 'include',
  })
  if (!res.ok) await throwApiError(res)
}

// ---------------------------------------------------------------------------
// User management (admin)
// ---------------------------------------------------------------------------

export async function listUsers(): Promise<UserListItem[]> {
  const res = await fetch('/api/users', { headers: buildAuthHeaders(), credentials: 'include' })
  if (!res.ok) await throwApiError(res)
  const data = await res.json()
  return data.users
}

export async function createUser(body: UserCreateRequest): Promise<UserListItem> {
  const res = await fetch('/api/users', {
    method: 'POST',
    headers: { ...buildAuthHeaders(), 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    credentials: 'include',
  })
  if (!res.ok) await throwApiError(res)
  return res.json()
}

export async function updateUser(id: number, body: UserUpdateRequest): Promise<UserListItem> {
  const res = await fetch(`/api/users/${id}`, {
    method: 'PATCH',
    headers: { ...buildAuthHeaders(), 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    credentials: 'include',
  })
  if (!res.ok) await throwApiError(res)
  return res.json()
}

export async function deleteUser(id: number): Promise<void> {
  const res = await fetch(`/api/users/${id}`, {
    method: 'DELETE',
    headers: buildAuthHeaders(),
    credentials: 'include',
  })
  if (!res.ok) await throwApiError(res)
}

export async function adminResetPassword(id: number, newPassword: string): Promise<void> {
  const res = await fetch(`/api/users/${id}/reset-password`, {
    method: 'POST',
    headers: { ...buildAuthHeaders(), 'Content-Type': 'application/json' },
    body: JSON.stringify({ new_password: newPassword }),
    credentials: 'include',
  })
  if (!res.ok) await throwApiError(res)
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

async function throwApiError(res: Response): Promise<never> {
  let detail = `${res.status} ${res.statusText}`
  try {
    const body = await res.json()
    if (body?.detail) detail = String(body.detail)
  } catch {
    /* keep status text */
  }
  throw new ApiError(res.status, detail)
}