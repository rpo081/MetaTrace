import { useEffect, useRef, useState } from 'react'

import { ApiError } from '../../api'
import {
  adminResetPassword,
  changeOwnPassword,
} from './api'
import { useAuth } from './AuthContext'

interface SelfProps {
  mode: 'self'
  dismissible: boolean
  onCancel?: () => void
}

interface AdminProps {
  mode: 'admin'
  userId: number
  username: string
  onCancel: () => void
  onDone?: () => void
}

type Props = SelfProps | AdminProps

const PASSWORD_HINT = '12+ chars, must include upper, lower, and a digit.'

function checkPasswordStrength(pw: string): string | null {
  if (pw.length < 12) return PASSWORD_HINT
  if (!/[A-Z]/.test(pw)) return PASSWORD_HINT
  if (!/[a-z]/.test(pw)) return PASSWORD_HINT
  if (!/[0-9]/.test(pw)) return PASSWORD_HINT
  return null
}

export function ChangePasswordModal(props: Props) {
  const isSelf = props.mode === 'self'
  const dismissible = isSelf ? props.dismissible : true
  const onCancel = isSelf ? (props.onCancel ?? (() => {})) : props.onCancel

  const auth = useAuth()

  const [current, setCurrent] = useState('')
  const [next, setNext] = useState('')
  const [confirm, setConfirm] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const dialogRef = useRef<HTMLDivElement | null>(null)
  const cancelRef = useRef<HTMLButtonElement | null>(null)
  const returnFocusRef = useRef<HTMLElement | null>(null)

  // Modal lifecycle: focus management, escape, focus trap, body scroll lock.
  useEffect(() => {
    returnFocusRef.current = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null

    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    cancelRef.current?.focus()

    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        if (!dismissible) return
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
  }, [dismissible, onCancel])

  const strengthError = next ? checkPasswordStrength(next) : null
  const matchError = confirm && next !== confirm ? 'Passwords do not match.' : null
  const canSubmit =
    !submitting &&
    next.length > 0 &&
    !strengthError &&
    !matchError &&
    (isSelf ? current.length > 0 : true)

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault()
    if (!canSubmit) return
    setError(null)
    setSubmitting(true)
    try {
      if (isSelf) {
        await auth.changePassword(current, next)
      } else if (props.mode === 'admin') {
        await adminResetPassword(props.userId, next)
        props.onDone?.()
      }
      // Close: parent (self mode) will unmount via state change in caller.
      // For admin mode, onDone callback is invoked above.
      if (isSelf) {
        // Self mode is mounted by ForceChangePasswordModal which watches the
        // flag; the flag was already cleared by auth.changePassword above.
        // No explicit close needed.
      }
      // Reset local form so re-opening (admin mode) starts fresh.
      setCurrent('')
      setNext('')
      setConfirm('')
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err.message)
      } else {
        setError(err instanceof Error ? err.message : String(err))
      }
    } finally {
      setSubmitting(false)
    }
  }

  const title = isSelf
    ? 'You must change your password'
    : `Reset password for ${props.mode === 'admin' ? props.username : ''}`
  const banner = isSelf
    ? 'For security reasons, you must set a new password before continuing.'
    : 'Admin reset — the user will need to re-login.'

  return (
    <div
      className="modal-backdrop"
      role="presentation"
      onMouseDown={(e) => {
        if (!dismissible) return
        if (e.target === e.currentTarget) onCancel()
      }}
    >
      <div
        ref={dialogRef}
        className="modal-card"
        role="dialog"
        aria-modal="true"
        aria-labelledby="change-password-title"
      >
        <div className="modal-header">
          <h2 id="change-password-title">{title}</h2>
        </div>
        <div className="info-box">{banner}</div>
        <form className="login-form" onSubmit={onSubmit}>
          {isSelf && (
            <label className="field" htmlFor="cp-current">
              <span className="field-label">Current password</span>
              <input
                id="cp-current"
                type="password"
                autoComplete="current-password"
                className="text-input"
                value={current}
                onChange={(e) => setCurrent(e.target.value)}
                disabled={submitting}
                required
              />
            </label>
          )}
          <label className="field" htmlFor="cp-new">
            <span className="field-label">New password</span>
            <input
              id="cp-new"
              type="password"
              autoComplete="new-password"
              className="text-input"
              value={next}
              onChange={(e) => setNext(e.target.value)}
              disabled={submitting}
              required
            />
            <span className="muted login-helper">{PASSWORD_HINT}</span>
          </label>
          <label className="field" htmlFor="cp-confirm">
            <span className="field-label">Confirm new password</span>
            <input
              id="cp-confirm"
              type="password"
              autoComplete="new-password"
              className="text-input"
              value={confirm}
              onChange={(e) => setConfirm(e.target.value)}
              disabled={submitting}
              required
            />
            {matchError && (
              <span className="error-box login-helper" role="alert">{matchError}</span>
            )}
          </label>
          {error && (
            <div className="error-box" role="alert" aria-live="polite">
              {error}
            </div>
          )}
          <div className="modal-actions">
            {dismissible && (
              <button
                ref={cancelRef}
                type="button"
                className="btn"
                onClick={onCancel}
                disabled={submitting}
              >
                Cancel
              </button>
            )}
            <button
              type="submit"
              className="btn btn-primary"
              disabled={!canSubmit}
            >
              {submitting ? 'Saving…' : 'Save password'}
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}