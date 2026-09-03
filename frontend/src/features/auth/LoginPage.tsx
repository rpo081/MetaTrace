import { useEffect, useRef, useState } from 'react'

import { ApiError } from '../../api'
import { useAuth } from './AuthContext'
import { MfaRequiredError } from './api'

declare const __APP_VERSION__: string

const ERROR_MESSAGES: Record<number, string> = {
  401: 'Invalid username or password.',
  423: 'Account temporarily locked. Try again later.',
  429: 'Too many attempts. Please wait a moment.',
}

const MFA_ERROR_MESSAGES: Record<number, string> = {
  401: 'Invalid code. Try again.',
  423: 'Account temporarily locked. Try again later.',
  429: 'Too many attempts. Please wait a moment.',
}

export function LoginPage() {
  const { login, loginWithMfa } = useAuth()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)

  // MFA Step-2 state — only populated when the server answers {mfa_required:true}.
  const [mfaToken, setMfaToken] = useState<string | null>(null)
  const [code, setCode] = useState('')
  const [useBackup, setUseBackup] = useState(false)

  const usernameRef = useRef<HTMLInputElement | null>(null)
  const codeRef = useRef<HTMLInputElement | null>(null)

  // Force-focus the username input on mount (plan-frontend §4.2).
  useEffect(() => {
    usernameRef.current?.focus()
  }, [])

  // When Step-2 appears, move focus to the code field.
  useEffect(() => {
    if (mfaToken) codeRef.current?.focus()
  }, [mfaToken])

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault()
    if (submitting) return
    setError(null)
    setSubmitting(true)
    try {
      await login(username, password)
      // AuthContext will flip state.status → 'authenticated' → <RequireAuth>
      // will swap in the main app automatically.
    } catch (err) {
      if (err instanceof MfaRequiredError) {
        // Password was correct — proceed to the second factor. No error shown.
        setMfaToken(err.mfaToken)
        setCode('')
        setUseBackup(false)
        setSubmitting(false)
        return
      }
      if (err instanceof ApiError) {
        const mapped = ERROR_MESSAGES[err.status]
        setError(mapped ?? err.message)
      } else {
        setError(err instanceof Error ? err.message : String(err))
      }
      setSubmitting(false)
    }
  }

  async function onMfaSubmit(e: React.FormEvent) {
    e.preventDefault()
    if (submitting || !mfaToken) return
    setError(null)
    setSubmitting(true)
    try {
      await loginWithMfa(mfaToken, code, useBackup)
    } catch (err) {
      if (err instanceof ApiError) {
        const mapped = MFA_ERROR_MESSAGES[err.status]
        setError(mapped ?? err.message)
      } else {
        setError(err instanceof Error ? err.message : String(err))
      }
      setSubmitting(false)
    }
  }

  function backToPassword() {
    setMfaToken(null)
    setCode('')
    setError(null)
    setUseBackup(false)
    setSubmitting(false)
  }

  if (mfaToken) {
    return (
      <div className="login-page">
        <form className="login-card" onSubmit={onMfaSubmit} aria-label="Two-factor verification">
          <div className="login-title-row">
            <h1 className="login-title">MetaTrace</h1>
            <span className="login-version">{__APP_VERSION__}</span>
          </div>
          <p className="muted login-subtitle">
            {useBackup ? 'Enter one of your backup codes.' : 'Enter the 6-digit code from your authenticator app.'}
          </p>

          <div className="login-form">
            <label className="field" htmlFor="login-mfa-code">
              <span className="field-label">{useBackup ? 'Backup code' : 'Authenticator code'}</span>
              <input
                id="login-mfa-code"
                ref={codeRef}
                type="text"
                inputMode={useBackup ? 'text' : 'numeric'}
                autoComplete="one-time-code"
                className="text-input mono"
                value={code}
                onChange={(e) => setCode(e.target.value)}
                disabled={submitting}
                required
                minLength={6}
                maxLength={32}
                placeholder={useBackup ? 'XXXX-XXXX-XXXX' : '123456'}
              />
            </label>

            {error && (
              <div className="error-box login-error" role="alert" aria-live="polite">
                {error}
              </div>
            )}

            <button
              type="submit"
              className="btn btn-primary login-submit"
              disabled={submitting || !code}
            >
              {submitting ? 'Verifying…' : 'Verify'}
            </button>
            <button
              type="button"
              className="btn"
              onClick={() => { setUseBackup((v) => !v); setCode(''); setError(null) }}
              disabled={submitting}
            >
              {useBackup ? 'Use authenticator code instead' : 'Use a backup code instead'}
            </button>
            <button
              type="button"
              className="btn"
              onClick={backToPassword}
              disabled={submitting}
            >
              Back to sign in
            </button>
          </div>
        </form>
      </div>
    )
  }

  return (
    <div className="login-page">
      <form className="login-card" onSubmit={onSubmit} aria-label="Sign in">
        <div className="login-title-row">
          <h1 className="login-title">MetaTrace</h1>
          <span className="login-version">{__APP_VERSION__}</span>
        </div>
        <p className="muted login-subtitle">Reverse image search</p>

        <div className="login-form">
          <label className="field" htmlFor="login-username">
            <span className="field-label">Username</span>
            <input
              id="login-username"
              ref={usernameRef}
              type="text"
              autoComplete="username"
              className="text-input"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              disabled={submitting}
              required
            />
          </label>

          <label className="field" htmlFor="login-password">
            <span className="field-label">Password</span>
            <input
              id="login-password"
              type="password"
              autoComplete="current-password"
              className="text-input"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              disabled={submitting}
              required
            />
          </label>

          {error && (
            <div className="error-box login-error" role="alert" aria-live="polite">
              {error}
            </div>
          )}

          <button
            type="submit"
            className="btn btn-primary login-submit"
            disabled={submitting || !username || !password}
          >
            {submitting ? 'Signing in…' : 'Sign in'}
          </button>
        </div>
      </form>
    </div>
  )
}
