import type { ReactNode } from 'react'

import { useAuth } from './AuthContext'
import { LoginPage } from './LoginPage'

export function RequireAuth({ children }: { children: ReactNode }) {
  const { state, loadingTooLong, retryBoot } = useAuth()

  if (state.status === 'loading') {
    return (
      <div className="login-page" role="status" aria-live="polite">
        <div className="login-card">
          <h1 className="login-title">MetaTrace</h1>
          {loadingTooLong ? (
            <>
              <p className="muted login-loading-text">
                Cannot reach server.
              </p>
              <button
                type="button"
                className="btn btn-primary login-submit"
                onClick={retryBoot}
              >
                Retry
              </button>
            </>
          ) : (
            <p className="muted login-loading-text">
              <span className="spinner" aria-hidden /> Loading…
            </p>
          )}
        </div>
      </div>
    )
  }

  if (state.status === 'unauthenticated') {
    return <LoginPage />
  }

  return <>{children}</>
}