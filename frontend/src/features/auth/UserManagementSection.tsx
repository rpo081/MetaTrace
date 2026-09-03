import { useCallback, useEffect, useState } from 'react'

import { ApiError } from '../../api'
import {
  adminResetMfa,
  deleteUser,
  listUsers,
  updateUser,
  type Role,
  type UserListItem,
} from './api'
import { AddUserModal } from './AddUserModal'
import { ChangePasswordModal } from './ChangePasswordModal'
import { useAuth } from './AuthContext'

interface Props {
  /** Optional override for the "current user". Defaults to the auth context user. */
  currentUserId?: number
}

export function UserManagementSection({ currentUserId }: Props) {
  const { state } = useAuth()
  const me = currentUserId ?? state.user?.id ?? null

  // Admin-only — render nothing for non-admins (plan-frontend §5.1).
  if (!state.user || state.user.role !== 'admin') {
    return null
  }

  return <UserManagementCard currentUserId={me} />
}

function UserManagementCard({ currentUserId }: { currentUserId: number | null }) {
  const [users, setUsers] = useState<UserListItem[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [addOpen, setAddOpen] = useState(false)
  const [resetTarget, setResetTarget] = useState<UserListItem | null>(null)
  const [busy, setBusy] = useState(false)

  const refresh = useCallback(async () => {
    setError(null)
    try {
      const list = await listUsers()
      setUsers(list)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    }
  }, [])

  const handleAddCancel = useCallback(() => setAddOpen(false), [])
  const handleAddCreated = useCallback(() => {
    setAddOpen(false)
    void refresh()
  }, [refresh])
  const handleResetCancel = useCallback(() => setResetTarget(null), [])
  const handleResetDone = useCallback(() => setResetTarget(null), [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  const activeAdminCount = (users ?? []).filter(
    (u) => u.role === 'admin' && u.is_active,
  ).length

  async function changeRole(u: UserListItem, role: Role) {
    setBusy(true)
    setError(null)
    try {
      await updateUser(u.id, { role })
      await refresh()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  async function toggleActive(u: UserListItem) {
    setBusy(true)
    setError(null)
    try {
      await updateUser(u.id, { is_active: !u.is_active })
      await refresh()
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err.message)
      } else {
        setError(err instanceof Error ? err.message : String(err))
      }
    } finally {
      setBusy(false)
    }
  }

  async function resetMfa(u: UserListItem) {
    if (!window.confirm(`Reset two-factor authentication for "${u.username}"? They can sign in with password only afterwards.`)) return
    setBusy(true)
    setError(null)
    try {
      await adminResetMfa(u.id)
      await refresh()
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err.message)
      } else {
        setError(err instanceof Error ? err.message : String(err))
      }
    } finally {
      setBusy(false)
    }
  }

  async function deleteRow(u: UserListItem) {
    if (u.id === currentUserId) return
    if (!window.confirm(`Delete user "${u.username}"? This cannot be undone.`)) return
    setBusy(true)
    setError(null)
    try {
      await deleteUser(u.id)
      await refresh()
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err.message)
      } else {
        setError(err instanceof Error ? err.message : String(err))
      }
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="settings-card">
      <div className="settings-header settings-header-row">
        <div>
          <h2>User Management</h2>
          <p className="muted">Manage local accounts and roles.</p>
        </div>
        <button
          type="button"
          className="btn btn-primary btn-sm"
          onClick={() => setAddOpen(true)}
          disabled={busy}
        >
          Add user
        </button>
      </div>

      {error && (
        <div className="error-box" role="alert" aria-live="polite">
          {error}
        </div>
      )}

      {users === null ? (
        <div className="muted">Loading…</div>
      ) : users.length === 0 ? (
        <div className="muted">No users yet.</div>
      ) : (
        <div className="user-mgmt-table-wrap">
          <table className="user-mgmt-table">
            <thead>
              <tr>
                <th scope="col">Username</th>
                <th scope="col">Role</th>
                <th scope="col">Active</th>
                <th scope="col">2FA</th>
                <th scope="col">Last login</th>
                <th scope="col">Actions</th>
              </tr>
            </thead>
            <tbody>
              {users.map((u) => {
                const isSelf = u.id === currentUserId
                const isLastActiveAdmin =
                  u.role === 'admin' && u.is_active && activeAdminCount <= 1
                const rowError = isLastActiveAdmin
                  ? 'At least 1 active admin must remain.'
                  : null
                return (
                  <tr key={u.id}>
                    <td>
                      {u.username}
                      {isSelf && (
                        <span className="user-mgmt-self-badge">you</span>
                      )}
                    </td>
                    <td>
                      <select
                        className="text-input user-mgmt-role-select"
                        value={u.role}
                        onChange={(e) => changeRole(u, e.target.value as Role)}
                        disabled={busy || isSelf || isLastActiveAdmin}
                        title={isSelf ? 'Cannot change your own role' : rowError ?? ''}
                        aria-label={`Role for ${u.username}`}
                      >
                        <option value="viewer">viewer</option>
                        <option value="editor">editor</option>
                        <option value="admin">admin</option>
                      </select>
                      {rowError && (
                        <span className="muted login-helper">{rowError}</span>
                      )}
                    </td>
                    <td>
                      <button
                        type="button"
                        className="btn btn-sm"
                        onClick={() => toggleActive(u)}
                        disabled={busy || isSelf || (u.is_active && isLastActiveAdmin)}
                        title={
                          isSelf
                            ? 'Cannot disable yourself'
                            : u.is_active && isLastActiveAdmin
                              ? rowError ?? ''
                              : u.is_active
                                ? 'Deactivate user'
                                : 'Activate user'
                        }
                        aria-label={`${u.is_active ? 'Deactivate' : 'Activate'} ${u.username}`}
                      >
                        {u.is_active ? 'Deactivate' : 'Activate'}
                      </button>
                    </td>
                    <td>
                      <span role="status" aria-label={`2FA for ${u.username}`}>
                        {u.mfa_enabled ? 'enabled' : '—'}
                      </span>
                    </td>
                    <td className="mono">
                      {u.last_login ? u.last_login.replace('T', ' ').replace('Z', '') : '—'}
                    </td>
                    <td>
                      <div className="user-mgmt-actions">
                        <button
                          type="button"
                          className="btn btn-sm"
                          onClick={() => setResetTarget(u)}
                          disabled={busy}
                          aria-label={`Reset password for ${u.username}`}
                        >
                          Reset password
                        </button>
                        {u.mfa_enabled && (
                          <button
                            type="button"
                            className="btn btn-sm"
                            onClick={() => resetMfa(u)}
                            disabled={busy}
                            aria-label={`Reset 2FA for ${u.username}`}
                          >
                            Reset 2FA
                          </button>
                        )}
                        <button
                          type="button"
                          className="btn btn-sm btn-danger-soft"
                          onClick={() => deleteRow(u)}
                          disabled={busy || isSelf}
                          title={isSelf ? 'Cannot delete yourself' : 'Delete user'}
                          aria-label={`Delete ${u.username}`}
                        >
                          Delete
                        </button>
                      </div>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}

      <AddUserModal
        open={addOpen}
        onCancel={handleAddCancel}
        onCreated={handleAddCreated}
      />

      {resetTarget && (
        <ChangePasswordModal
          mode="admin"
          userId={resetTarget.id}
          username={resetTarget.username}
          onCancel={handleResetCancel}
          onDone={handleResetDone}
        />
      )}
    </section>
  )
}