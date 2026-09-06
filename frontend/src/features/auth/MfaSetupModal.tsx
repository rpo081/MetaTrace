import { useCallback, useEffect, useRef, useState } from 'react'

import { ApiError } from '../../api'
import { useAuth } from './AuthContext'
import {
  mfaConfirm,
  mfaDisable,
  mfaEnroll,
  mfaQrBlob,
  mfaRegenerateCodes,
  mfaStatus,
  type MfaStatus,
} from './api'

interface Props {
  onCancel: () => void
  onChanged?: () => void
}

type Step = 'loading' | 'overview' | 'enrolling' | 'backup' | 'disable' | 'regenerate'

export function MfaSetupModal({ onCancel, onChanged }: Props) {
  // Destructure only the stable setter: AuthProvider rebuilds its context
  // object every render, so depending on the whole `auth` object would give
  // `refresh` a new identity each render and re-fire the mount effect below
  // into an infinite status-poll loop.
  const { setMfaEnabled } = useAuth()
  const [step, setStep] = useState<Step>('loading')
  const [status, setStatus] = useState<MfaStatus | null>(null)
  const [secret, setSecret] = useState<string | null>(null)
  const [qrUrl, setQrUrl] = useState<string | null>(null)
  const [code, setCode] = useState('')
  const [password, setPassword] = useState('')
  const [backupCodes, setBackupCodes] = useState<string[]>([])
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)

  const dialogRef = useRef<HTMLDivElement | null>(null)
  const cancelRef = useRef<HTMLButtonElement | null>(null)
  const returnFocusRef = useRef<HTMLElement | null>(null)
  const onCancelRef = useRef(onCancel)
  useEffect(() => {
    onCancelRef.current = onCancel
  }, [onCancel])

  const refresh = useCallback(async () => {
    setError(null)
    try {
      const s = await mfaStatus()
      setStatus(s)
      setMfaEnabled(s.enabled)
      setStep('overview')
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
      setStep('overview')
    }
  }, [setMfaEnabled])

  useEffect(() => {
    void refresh()
  }, [refresh])

  // Revoke the QR blob URL when it changes or the modal unmounts.
  useEffect(() => {
    return () => {
      if (qrUrl) URL.revokeObjectURL(qrUrl)
    }
  }, [qrUrl])

  // Move focus into the step's form on user-initiated step changes
  // (ChangePasswordModal pattern: focus-first-input; the mount effect above
  // already focused Close for the input-less overview).
  useEffect(() => {
    if (step === 'enrolling' || step === 'disable' || step === 'regenerate') {
      dialogRef.current?.querySelector<HTMLInputElement>('form input:not([disabled])')?.focus()
    }
  }, [step])

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
        e.preventDefault()
        onCancelRef.current()
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
  }, [])

  async function startEnroll() {
    // Rotating the pending secret is privileged: the server re-authenticates
    // the password (POST /enroll {password}).
    if (submitting || !password) return
    setError(null)
    setSubmitting(true)
    try {
      const res = await mfaEnroll(password)
      setSecret(res.secret)
      setCode('')
      try {
        const blob = await mfaQrBlob()
        // Revoke outside the state updater (updaters must be pure —
        // StrictMode double-invokes them, which would leak an object URL).
        if (qrUrl) URL.revokeObjectURL(qrUrl)
        setQrUrl(URL.createObjectURL(blob))
      } catch {
        setQrUrl(null)
      }
      setStep('enrolling')
    } catch (err) {
      if (err instanceof ApiError && err.status === 410) {
        // Refresh first (it clears errors), then set the expiry message so
        // it survives the status reload and shows on the overview step.
        await refresh()
        setError('Enrollment expired — start over with a fresh code.')
      } else {
        setError(err instanceof Error ? err.message : String(err))
      }
    } finally {
      setSubmitting(false)
    }
  }

  async function resumeEnroll() {
    // Continue an unfinished enrollment: re-fetch the QR for the existing
    // pending secret instead of rotating it. The manual-entry key is unknown
    // here (only POST /enroll returns it), so just the QR is shown — the
    // pending secret never changes.
    //
    // Viewing the existing QR needs no password, but binding it via confirm
    // does (POST /confirm {code, password}), so a hijacked session can view
    // but cannot persist an attacker factor without the password. Rotating
    // via POST /enroll always re-authenticates the password. Only a 404
    // (pending state gone server-side) falls back to a fresh enroll; any
    // other QR failure is surfaced so a transient error never silently rotates.
    if (submitting) return
    setError(null)
    setSubmitting(true)
    try {
      const blob = await mfaQrBlob()
      // Revoke outside the state updater (updaters must be pure —
      // StrictMode double-invokes them, which would leak an object URL).
      if (qrUrl) URL.revokeObjectURL(qrUrl)
      setQrUrl(URL.createObjectURL(blob))
      setSecret(null)
      setCode('')
      setStep('enrolling')
      setSubmitting(false)
    } catch (err) {
      if (err instanceof ApiError && err.status === 410) {
        setSubmitting(false)
        await refresh()
        setError('Enrollment expired — enter your password and start over.')
        return
      }
      if (err instanceof ApiError && err.status === 404) {
        setSubmitting(false)
        if (!password) {
          setError('Pending enrollment is gone — enter your password and start over.')
          return
        }
        await startEnroll()
        return
      }
      setSubmitting(false)
      setError(err instanceof Error ? err.message : String(err))
    }
  }

  async function confirmEnroll(e: React.FormEvent) {
    e.preventDefault()
    if (submitting || !code || !password) return
    setError(null)
    setSubmitting(true)
    try {
      const res = await mfaConfirm(code, password)
      setBackupCodes(res.backup_codes)
      // Stay on the backup step until the user clicks Done — do NOT refresh
      // here (refresh flips back to 'overview' and the once-displayed codes
      // would never be shown).
      // The password served its re-auth purpose — drop it now so it does not
      // linger in memory (leaveBackupStep clears it again on Done).
      setPassword('')
      setMfaEnabled(true)
      setStep('backup')
      onChanged?.()
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        setError('Invalid code. Try again.')
      } else if (err instanceof ApiError && err.status === 400) {
        // 400 is the wrong-password re-auth failure in the normal flow, but
        // the backend also uses it for raced states (already enabled /
        // pending gone) — surface those verbatim instead of mislabeling.
        setError(/password/i.test(err.message) ? 'Current password is incorrect.' : err.message)
      } else if (err instanceof ApiError && err.status === 410) {
        await refresh()
        setError('Enrollment expired — start over with a fresh code.')
      } else {
        setError(err instanceof Error ? err.message : String(err))
      }
    } finally {
      setSubmitting(false)
    }
  }

  async function confirmDisable(e: React.FormEvent) {
    e.preventDefault()
    if (submitting || !password) return
    setError(null)
    setSubmitting(true)
    try {
      await mfaDisable(password, status?.enabled ? code || undefined : undefined)
      setPassword('')
      setCode('')
      onChanged?.()
      await refresh()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setSubmitting(false)
    }
  }

  async function confirmRegenerate(e: React.FormEvent) {
    e.preventDefault()
    if (submitting || !code || !password) return
    setError(null)
    setSubmitting(true)
    try {
      const res = await mfaRegenerateCodes(code, password)
      setBackupCodes(res.backup_codes)
      // Same as confirm: stay on backup until Done (see confirmEnroll).
      // Password served its re-auth purpose — drop it (leaveBackupStep
      // clears it again on Done).
      setPassword('')
      setStep('backup')
      onChanged?.()
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        setError('Invalid code. Try again.')
      } else {
        setError(err instanceof Error ? err.message : String(err))
      }
    } finally {
      setSubmitting(false)
    }
  }

  function leaveBackupStep() {
    // Codes were shown once — drop them (and the pending secret/QR) so they
    // don't linger in memory, then reload the true status.
    setBackupCodes([])
    setCode('')
    setPassword('')
    setSecret(null)
    if (qrUrl) {
      URL.revokeObjectURL(qrUrl)
      setQrUrl(null)
    }
    void refresh()
  }

  function backToOverview() {
    setCode('')
    setPassword('')
    setError(null)
    void refresh()
  }

  function copyBackupCodes() {
    void navigator.clipboard?.writeText(backupCodes.join('\n')).catch(() => {})
  }

  function downloadBackupCodes() {
    const blob = new Blob([backupCodes.join('\n')], { type: 'text/plain' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = 'metatrace-backup-codes.txt'
    a.click()
    URL.revokeObjectURL(url)
  }

  const title = 'Two-factor authentication'

  return (
    <div
      className="modal-backdrop"
      role="presentation"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onCancelRef.current()
      }}
    >
      <div
        ref={dialogRef}
        className="modal-card"
        role="dialog"
        aria-modal="true"
        aria-labelledby="mfa-setup-title"
      >
        <div className="modal-header">
          <h2 id="mfa-setup-title">{title}</h2>
        </div>

        {step === 'loading' && <div className="muted">Loading…</div>}

        {step === 'overview' && (
          <div className="login-form">
            <div className="info-box">
              {status?.enabled
                ? `Two-factor authentication is enabled. ${status.backup_remaining} unused backup codes remain.`
                : status?.has_pending
                  ? 'Two-factor authentication is disabled. You have an unfinished enrollment — continue where you left off or start over.'
                  : 'Two-factor authentication is disabled. Enable it with an authenticator app (TOTP).'}
            </div>
            {status?.enabled && status.backup_remaining <= 2 && (
              <div className="warning-box" role="status">
                Only {status.backup_remaining} unused backup {status.backup_remaining === 1 ? 'code remains' : 'codes remain'}.
                Generate a new set soon so you are not locked out if you lose your authenticator.
              </div>
            )}
            {status?.pending_expired && !status.enabled && (
              <div className="warning-box" role="status">
                Enrollment expired — enter your password and start over with a fresh code.
              </div>
            )}
            {!status?.enabled && (
              <label className="field" htmlFor="mfa-overview-password">
                <span className="field-label">Current password (required to start over or enable)</span>
                <input
                  id="mfa-overview-password"
                  type="password"
                  autoComplete="current-password"
                  className="text-input"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  disabled={submitting}
                  placeholder="Your password"
                />
              </label>
            )}
            {error && (
              <div className="error-box" role="alert" aria-live="polite">{error}</div>
            )}
            <div className="modal-actions">
              {!status?.enabled && !status?.has_pending && !status?.pending_expired && (
                <button
                  type="button"
                  className="btn btn-primary"
                  onClick={() => void startEnroll()}
                  disabled={submitting || !password}
                >
                  {submitting ? 'Starting…' : 'Enable 2FA'}
                </button>
              )}
              {!status?.enabled && (status?.has_pending || status?.pending_expired) && (
                <>
                  {status?.has_pending && (
                    <button
                      type="button"
                      className="btn btn-primary"
                      onClick={() => void resumeEnroll()}
                      disabled={submitting}
                    >
                      {submitting ? 'Loading…' : 'Continue enrollment'}
                    </button>
                  )}
                  <button
                    type="button"
                    className="btn"
                    onClick={() => void startEnroll()}
                    disabled={submitting || !password}
                  >
                    {submitting ? 'Starting…' : 'Start over'}
                  </button>
                </>
              )}
              {status?.enabled && (
                <>
                  <button type="button" className="btn" onClick={() => { setCode(''); setPassword(''); setError(null); setStep('regenerate') }}>
                    New backup codes
                  </button>
                  <button type="button" className="btn btn-danger-soft" onClick={() => { setCode(''); setPassword(''); setError(null); setStep('disable') }}>
                    Disable 2FA
                  </button>
                </>
              )}
            </div>
          </div>
        )}

        {step === 'enrolling' && (
          <form className="login-form" onSubmit={confirmEnroll}>
            <div className="info-box">
              Scan the QR code with your authenticator app, then confirm with
              your password and the 6-digit code.
            </div>
            {qrUrl && (
              <img src={qrUrl} alt="QR code for authenticator enrollment" className="mfa-qr" />
            )}
            {secret && (
              <div className="muted">Manual entry key: <span className="mono">{secret}</span></div>
            )}
            <label className="field" htmlFor="mfa-confirm-password">
              <span className="field-label">Current password</span>
              <input
                id="mfa-confirm-password"
                type="password"
                autoComplete="current-password"
                className="text-input"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                disabled={submitting}
                required
                placeholder="Your password"
              />
            </label>
            <label className="field" htmlFor="mfa-confirm-code">
              <span className="field-label">Authenticator code</span>
              <input
                id="mfa-confirm-code"
                type="text"
                inputMode="numeric"
                autoComplete="one-time-code"
                className="text-input mono"
                value={code}
                onChange={(e) => setCode(e.target.value)}
                disabled={submitting}
                required
                minLength={6}
                maxLength={16}
                placeholder="123456"
              />
            </label>
            {error && (
              <div className="error-box" role="alert" aria-live="polite">{error}</div>
            )}
            <div className="modal-actions">
              <button type="submit" className="btn btn-primary" disabled={submitting || !code || !password}>
                {submitting ? 'Confirming…' : 'Confirm'}
              </button>
              {error?.toLowerCase().includes('expired') && (
                <button type="button" className="btn" onClick={backToOverview} disabled={submitting}>
                  Start over
                </button>
              )}
            </div>
          </form>
        )}

        {step === 'backup' && (
          <div className="login-form">
            <div className="info-box">
              Save these backup codes now — each works once if you lose your authenticator.
              They will not be shown again.
            </div>
            <ul className="mfa-backup-list mono">
              {backupCodes.map((c) => (
                <li key={c}>{c}</li>
              ))}
            </ul>
            <div className="modal-actions">
              <button type="button" className="btn" onClick={copyBackupCodes}>Copy</button>
              <button type="button" className="btn" onClick={downloadBackupCodes}>Download</button>
              <button
                type="button"
                className="btn btn-primary"
                onClick={leaveBackupStep}
              >
                Done
              </button>
            </div>
          </div>
        )}

        {step === 'disable' && (
          <form className="login-form" onSubmit={confirmDisable}>
            <div className="info-box">Disable two-factor authentication. Your password is required.</div>
            <label className="field" htmlFor="mfa-disable-password">
              <span className="field-label">Current password</span>
              <input
                id="mfa-disable-password"
                type="password"
                autoComplete="current-password"
                className="text-input"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                disabled={submitting}
                required
              />
            </label>
            {status?.enabled && (
              <label className="field" htmlFor="mfa-disable-code">
                <span className="field-label">Authenticator or backup code</span>
                <input
                  id="mfa-disable-code"
                  type="text"
                  inputMode="numeric"
                  autoComplete="one-time-code"
                  className="text-input mono"
                  value={code}
                  onChange={(e) => setCode(e.target.value)}
                  disabled={submitting}
                  required
                  minLength={6}
                  maxLength={32}
                />
              </label>
            )}
            {error && (
              <div className="error-box" role="alert" aria-live="polite">{error}</div>
            )}
            <div className="modal-actions">
              <button type="submit" className="btn btn-primary" disabled={submitting || !password}>
                {submitting ? 'Disabling…' : 'Disable 2FA'}
              </button>
              <button type="button" className="btn" onClick={backToOverview} disabled={submitting}>
                Back
              </button>
            </div>
          </form>
        )}

        {step === 'regenerate' && (
          <form className="login-form" onSubmit={confirmRegenerate}>
            <div className="info-box">
              Generate a new set of backup codes. The old set stops working immediately.
              Your password and an authenticator code are both required.
            </div>
            <label className="field" htmlFor="mfa-regen-password">
              <span className="field-label">Current password</span>
              <input
                id="mfa-regen-password"
                type="password"
                autoComplete="current-password"
                className="text-input"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                disabled={submitting}
                required
              />
            </label>
            <label className="field" htmlFor="mfa-regen-code">
              <span className="field-label">Authenticator code</span>
              <input
                id="mfa-regen-code"
                type="text"
                inputMode="numeric"
                autoComplete="one-time-code"
                className="text-input mono"
                value={code}
                onChange={(e) => setCode(e.target.value)}
                disabled={submitting}
                required
                minLength={6}
                maxLength={16}
              />
            </label>
            {error && (
              <div className="error-box" role="alert" aria-live="polite">{error}</div>
            )}
            <div className="modal-actions">
              <button type="submit" className="btn btn-primary" disabled={submitting || !code || !password}>
                {submitting ? 'Generating…' : 'Generate new codes'}
              </button>
              <button type="button" className="btn" onClick={backToOverview} disabled={submitting}>
                Back
              </button>
            </div>
          </form>
        )}

        <div className="modal-actions">
          <button ref={cancelRef} type="button" className="btn" onClick={onCancel} disabled={submitting}>
            Close
          </button>
        </div>
      </div>
    </div>
  )
}
