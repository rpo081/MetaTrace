import { useEffect, useRef, useState } from 'react'

import { ApiError } from '../../api'
import { createUser, type Role, type UserCreateRequest, type UserListItem } from './api'

interface Props {
  open: boolean
  onCancel: () => void
  onCreated: (user: UserListItem) => void
}

const PASSWORD_HINT = '12+ chars, must include upper, lower, and a digit.'

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/
const USERNAME_RE = /^[a-zA-Z0-9_-]+$/

function validateField(values: {
  username: string
  email: string
  password: string
}): Partial<Record<keyof UserCreateRequest, string>> {
  const errors: Partial<Record<keyof UserCreateRequest, string>> = {}
  if (!USERNAME_RE.test(values.username) || values.username.length < 3 || values.username.length > 64) {
    errors.username = '3–64 chars, letters / digits / _- only.'
  }
  if (!EMAIL_RE.test(values.email)) {
    errors.email = 'Enter a valid email address.'
  }
  if (values.password.length < 12) {
    errors.password = PASSWORD_HINT
  } else if (!/[A-Z]/.test(values.password)) {
    errors.password = PASSWORD_HINT
  } else if (!/[a-z]/.test(values.password)) {
    errors.password = PASSWORD_HINT
  } else if (!/[0-9]/.test(values.password)) {
    errors.password = PASSWORD_HINT
  }
  return errors
}

export function AddUserModal({ open, onCancel, onCreated }: Props) {
  const [username, setUsername] = useState('')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [role, setRole] = useState<Role>('viewer')
  const [submitting, setSubmitting] = useState(false)
  const [errors, setErrors] = useState<Partial<Record<keyof UserCreateRequest, string>>>({})
  const [topError, setTopError] = useState<string | null>(null)

  const dialogRef = useRef<HTMLDivElement | null>(null)
  const cancelRef = useRef<HTMLButtonElement | null>(null)
  const returnFocusRef = useRef<HTMLElement | null>(null)

  useEffect(() => {
    if (!open) return
    returnFocusRef.current = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null

    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    cancelRef.current?.focus()

    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault()
        onCancel()
        return
      }
      if (e.key !== 'Tab') return
      const focusables = dialogRef.current?.querySelectorAll<HTMLElement>(
        'button:not([disabled]), input:not([disabled]), [href], select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
      )
      if (!focusables || focusables.length === 0) return
      const first = focusables[0]
      const last = focusables[focusables.length - 1]
      const active = document.activeElement
      if (e.shiftKey && active === first) {
        e.preventDefault()
        last.focus()
      } else if (!e.shiftKey && active === last) {
        e.preventDefault()
        first.focus()
      }
    }
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.body.style.overflow = previousOverflow
      document.removeEventListener('keydown', onKeyDown)
      returnFocusRef.current?.focus()
      returnFocusRef.current = null
    }
  }, [open, onCancel])

  if (!open) return null

  function reset() {
    setUsername('')
    setEmail('')
    setPassword('')
    setRole('viewer')
    setErrors({})
    setTopError(null)
  }

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault()
    const fieldErrors = validateField({ username, email, password })
    setErrors(fieldErrors)
    if (Object.keys(fieldErrors).length > 0) return

    setSubmitting(true)
    setTopError(null)
    try {
      const user = await createUser({ username, email, password, role })
      onCreated(user)
      reset()
    } catch (err) {
      if (err instanceof ApiError) {
        if (err.status === 409) {
          setTopError(
            err.message.includes('username')
              ? 'Username is already taken.'
              : err.message.includes('email')
                ? 'Email is already registered.'
                : err.message,
          )
        } else if (err.status === 429) {
          setTopError('Too many requests. Please wait a moment.')
        } else {
          setTopError(err.message)
        }
      } else {
        setTopError(err instanceof Error ? err.message : String(err))
      }
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div
      className="modal-backdrop"
      role="presentation"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onCancel()
      }}
    >
      <div
        ref={dialogRef}
        className="modal-card"
        role="dialog"
        aria-modal="true"
        aria-labelledby="add-user-title"
      >
        <div className="modal-header">
          <h2 id="add-user-title">Add user</h2>
        </div>
        <form className="login-form" onSubmit={onSubmit}>
          <label className="field" htmlFor="add-username">
            <span className="field-label">Username</span>
            <input
              id="add-username"
              type="text"
              className="text-input"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              disabled={submitting}
              autoComplete="off"
            />
            {errors.username && (
              <span className="error-box login-helper" role="alert">{errors.username}</span>
            )}
          </label>

          <label className="field" htmlFor="add-email">
            <span className="field-label">Email</span>
            <input
              id="add-email"
              type="email"
              className="text-input"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              disabled={submitting}
              autoComplete="off"
            />
            {errors.email && (
              <span className="error-box login-helper" role="alert">{errors.email}</span>
            )}
          </label>

          <label className="field" htmlFor="add-password">
            <span className="field-label">Password</span>
            <input
              id="add-password"
              type="password"
              className="text-input"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              disabled={submitting}
              autoComplete="new-password"
            />
            <span className="muted login-helper">{PASSWORD_HINT}</span>
            {errors.password && (
              <span className="error-box login-helper" role="alert">{errors.password}</span>
            )}
          </label>

          <label className="field" htmlFor="add-role">
            <span className="field-label">Role</span>
            <select
              id="add-role"
              className="text-input"
              value={role}
              onChange={(e) => setRole(e.target.value as Role)}
              disabled={submitting}
            >
              <option value="viewer">viewer</option>
              <option value="editor">editor</option>
              <option value="admin">admin</option>
            </select>
          </label>

          {topError && (
            <div className="error-box" role="alert" aria-live="polite">
              {topError}
            </div>
          )}

          <div className="modal-actions">
            <button
              ref={cancelRef}
              type="button"
              className="btn"
              onClick={onCancel}
              disabled={submitting}
            >
              Cancel
            </button>
            <button
              type="submit"
              className="btn btn-primary"
              disabled={submitting || !username || !email || !password}
            >
              {submitting ? 'Creating…' : 'Create user'}
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}