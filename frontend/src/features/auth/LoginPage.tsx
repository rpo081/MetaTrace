import { useEffect, useRef, useState } from 'react'

import { ApiError } from '../../api'
import { useAuth } from './AuthContext'

declare const __APP_VERSION__: string

const ERROR_MESSAGES: Record<number, string> = {
  401: 'Invalid username or password.',
  423: 'Account temporarily locked. Try again later.',
  429: 'Too many attempts. Please wait a moment.',
}

export function LoginPage() {
  const { login } = useAuth()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)

  const usernameRef = useRef<HTMLInputElement | null>(null)

  // Force-focus the username input on mount (plan-frontend §4.2).
  useEffect(() => {
    usernameRef.current?.focus()
  }, [])

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
      if (err instanceof ApiError) {
        const mapped = ERROR_MESSAGES[err.status]
        setError(mapped ?? err.message)
      } else {
        setError(err instanceof Error ? err.message : String(err))
      }
      setSubmitting(false)
    }
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