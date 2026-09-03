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
    if (submitting) return
    setError(null)
    setSubmitting(true)
    try {
      const res = await mfaEnroll()
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
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setSubmitting(false)
    }
  }

  async function confirmEnroll(e: React.FormEvent) {
    e.preventDefault()
    if (submitting || !code) return
    setError(null)
    setSubmitting(true)
    try {
      const res = await mfaConfirm(code)
      setBackupCodes(res.backup_codes)
      // Stay on the backup step until the user clicks Done — do NOT refresh
      // here (refresh flips back to 'overview' and the once-displayed codes
      // would never be shown).
      setMfaEnabled(true)
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
    if (submitting || !code) return
    setError(null)
    setSubmitting(true)
    try {
      const res = await mfaRegenerateCodes(code)
      setBackupCodes(res.backup_codes)
      // Same as confirm: stay on backup until Done (see confirmEnroll).
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
                : 'Two-factor authentication is disabled. Enable it with an authenticator app (TOTP).'}
            </div>
            {error && (
              <div className="error-box" role="alert" aria-live="polite">{error}</div>
            )}
            <div className="modal-actions">
              {!status?.enabled && (
                <button type="button" className="btn btn-primary" onClick={() => void startEnroll()} disabled={submitting}>
                  {submitting ? 'Starting…' : 'Enable 2FA'}
                </button>
              )}
              {status?.enabled && (
                <>
                  <button type="button" className="btn" onClick={() => { setCode(''); setError(null); setStep('regenerate') }}>
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
              Scan the QR code with your authenticator app, then enter the 6-digit code to confirm.
            </div>
            {qrUrl && (
              <img src={qrUrl} alt="QR code for authenticator enrollment" className="mfa-qr" />
            )}
            {secret && (
              <div className="muted">Manual entry key: <span className="mono">{secret}</span></div>
            )}
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
              <button type="submit" className="btn btn-primary" disabled={submitting || !code}>
                {submitting ? 'Confirming…' : 'Confirm'}
              </button>
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
            </div>
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
              <button type="submit" className="btn btn-primary" disabled={submitting || !code}>
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
