import { render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { UserListItem } from '../api'
import { UserManagementSection } from '../UserManagementSection'
import type { AuthApi } from '../AuthContext'

const adminUser: UserListItem = {
  id: 1, username: 'admin', email: 'admin@example.com', role: 'admin',
  is_active: true, created_at: '2026-01-01T00:00:00Z', last_login: null,
}

const usersFixture: UserListItem[] = [
  adminUser,
  {
    id: 2, username: 'eve', email: 'eve@example.com', role: 'editor',
    is_active: true, created_at: '2026-01-01T00:00:00Z', last_login: '2026-01-02T03:04:05Z',
  },
  {
    id: 3, username: 'vin', email: 'vin@example.com', role: 'viewer',
    is_active: false, created_at: '2026-01-01T00:00:00Z', last_login: null,
  },
]

let authMock: Partial<AuthApi> & { state: AuthApi['state'] }
let fetchMock: ReturnType<typeof vi.fn>

vi.mock('../AuthContext', async () => {
  const actual = await vi.importActual<typeof import('../AuthContext')>('../AuthContext')
  return {
    ...actual,
    useAuth: () => authMock,
  }
})

beforeEach(() => {
  authMock = {
    state: {
      user: { ...adminUser, role: 'admin' },
      status: 'authenticated',
      mustChangePassword: false,
    },
    loadingTooLong: false,
    login: vi.fn(),
    logout: vi.fn(),
    refresh: vi.fn(),
    changePassword: vi.fn(),
    retryBoot: vi.fn(),
    withAuthRetry: vi.fn(),
  }
  fetchMock = vi.fn()
  globalThis.fetch = fetchMock as unknown as typeof fetch
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('UserManagementSection', () => {
  it('renders_table_with_users', async () => {
    fetchMock.mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ users: usersFixture }),
    } as unknown as Response)

    render(<UserManagementSection currentUserId={1} />)

    // Header
    expect(screen.getByText(/user management/i)).toBeInTheDocument()

    // Wait for the user list to be fetched and rendered.
    await waitFor(() => {
      // Look up by row, not by raw "admin" — the username also appears
      // inside the role <select>'s <option> tags.
      expect(screen.getByText('eve')).toBeInTheDocument()
    })

    expect(screen.getByText('vin')).toBeInTheDocument()

    // "you" badge on the self row
    expect(screen.getByText(/you/)).toBeInTheDocument()

    // Per-row actions
    const resetBtns = screen.getAllByLabelText(/reset password for/i)
    expect(resetBtns).toHaveLength(3)
  })

  it('admin_only_render', async () => {
    authMock = {
      ...authMock,
      state: {
        user: { ...adminUser, id: 2, role: 'viewer' },
        status: 'authenticated',
        mustChangePassword: false,
      },
    }

    render(<UserManagementSection currentUserId={2} />)
    expect(screen.queryByText(/user management/i)).toBeNull()
    expect(screen.queryByRole('table')).toBeNull()
  })

  it('last_admin_role_select_disabled', async () => {
    // Only one admin (admin) — and that admin is the current viewer.
    fetchMock.mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ users: [adminUser] }),
    } as unknown as Response)

    render(<UserManagementSection currentUserId={1} />)

    await waitFor(() => {
      expect(screen.getByLabelText(/role for admin/i)).toBeInTheDocument()
    })

    // Self row → role select disabled (Cannot change your own role)
    const roleSelect = screen.getByLabelText(/role for admin/i) as HTMLSelectElement
    expect(roleSelect).toBeDisabled()
    // The delete button on the self row is also disabled.
    const deleteBtn = screen.getByLabelText(/delete admin/i)
    expect(deleteBtn).toBeDisabled()
  })
})