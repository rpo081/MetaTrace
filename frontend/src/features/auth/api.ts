// Thin wrappers around /api/auth/* and /api/users/*.
// Reuses the existing `ApiError` from ../../api.ts so every call site gets
// identical status-code semantics for free. The `authHeaders()` helper in
// ../../api.ts is module-private — we build our own here that mirrors its
// behavior exactly (Bearer access token first, then legacy admin token).
// Key constants match ../../api.ts so a token written via either path is
// readable from both.

import { ApiError } from '../../api'

const ACCESS_TOKEN_KEY = 'metatrace_access_token'
const ADMIN_TOKEN_KEY = 'metatrace_admin_token'

export function getAccessToken(): string | null {
  try {
    return localStorage.getItem(ACCESS_TOKEN_KEY)
  } catch {
    return null
  }
}

export function setAccessToken(token: string | null): void {
  try {
    if (token) localStorage.setItem(ACCESS_TOKEN_KEY, token)
    else localStorage.removeItem(ACCESS_TOKEN_KEY)
  } catch {
    /* ignore — storage unavailable (privacy mode etc.) */
  }
}

function getAdminToken(): string | null {
  try {
    return localStorage.getItem(ADMIN_TOKEN_KEY)
  } catch {
    return null
  }
}

function buildAuthHeaders(): Record<string, string> {
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
  email: string
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
  return null
}

export async function fetchMe(): Promise<MeResponse> {
  const res = await fetch('/api/auth/me', { headers: buildAuthHeaders() })
  if (!res.ok) await throwApiError(res)
  return res.json()
}

export async function changeOwnPassword(current: string, next: string): Promise<void> {
  const res = await fetch('/api/auth/change-password', {
    method: 'POST',
    headers: { ...buildAuthHeaders(), 'Content-Type': 'application/json' },
    body: JSON.stringify({ current_password: current, new_password: next }),
  })
  if (!res.ok) await throwApiError(res)
}

// ---------------------------------------------------------------------------
// User management (admin)
// ---------------------------------------------------------------------------

export async function listUsers(): Promise<UserListItem[]> {
  const res = await fetch('/api/users', { headers: buildAuthHeaders() })
  if (!res.ok) await throwApiError(res)
  const data = await res.json()
  return data.users
}

export async function createUser(body: UserCreateRequest): Promise<UserListItem> {
  const res = await fetch('/api/users', {
    method: 'POST',
    headers: { ...buildAuthHeaders(), 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) await throwApiError(res)
  return res.json()
}

export async function updateUser(id: number, body: UserUpdateRequest): Promise<UserListItem> {
  const res = await fetch(`/api/users/${id}`, {
    method: 'PATCH',
    headers: { ...buildAuthHeaders(), 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) await throwApiError(res)
  return res.json()
}

export async function deleteUser(id: number): Promise<void> {
  const res = await fetch(`/api/users/${id}`, {
    method: 'DELETE',
    headers: buildAuthHeaders(),
  })
  if (!res.ok) await throwApiError(res)
}

export async function adminResetPassword(id: number, newPassword: string): Promise<void> {
  const res = await fetch(`/api/users/${id}/reset-password`, {
    method: 'POST',
    headers: { ...buildAuthHeaders(), 'Content-Type': 'application/json' },
    body: JSON.stringify({ new_password: newPassword }),
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