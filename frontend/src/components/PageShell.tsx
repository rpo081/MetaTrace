/** PageShell — topbar (brand, scan controls, navigation, logout), global scan
  *  status line, and the FullRescan modal. Owns the page nav state and forwards
  *  it via a callback. Children are rendered below the global status slot.
  */
import {
  useCallback,
  useEffect,
  type ReactNode,
} from 'react'
import type { AppPage, Stats } from '../types'
import { SearchIcon, GridIcon, GearIcon, LogoutIcon } from './Icon'
import ScanReportLine, { getReportRates } from './ScanReportLine'
import { useAuth } from '../features/auth/AuthContext'
import type { StatsErrorKind } from '../hooks/useStatsPolling'

function fmtRate(val: number | undefined): string {
  if (val == null || val <= 0) return '0'
  if (val >= 100) return `${Math.round(val)}`
  return `${Math.round(val * 10) / 10}`
}

function NavIcon({ page }: { page: AppPage }) {
  const iconClass = "nav-btn-icon"
  if (page === 'search') return <SearchIcon className={iconClass} />
  if (page === 'browse') return <GridIcon className={iconClass} />
  return <GearIcon className={iconClass} />
}

function FullRescanModal({
  open,
  confirmed,
  onConfirmedChange,
  onCancel,
  onConfirm,
  cancelRef,
  dialogRef,
}: {
  open: boolean
  confirmed: boolean
  onConfirmedChange: (value: boolean) => void
  onCancel: () => void
  onConfirm: () => void
  cancelRef: { current: HTMLButtonElement | null }
  dialogRef: { current: HTMLDivElement | null }
}) {
  if (!open) return null

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
        aria-labelledby="full-rescan-title"
        aria-describedby="full-rescan-description"
      >
        <div className="modal-header">
          <h2 id="full-rescan-title">Start full scan and reset indexed data?</h2>
        </div>
        <div id="full-rescan-description" className="modal-copy">
          <p>This will initialize the model, reset existing indexed entries, and rebuild the image index from scratch.</p>
          <ul className="modal-list">
            <li>Existing indexed and searchable entries will be replaced.</li>
            <li>Search and browse results may be incomplete until the scan finishes.</li>
            <li>The first step may take longer while the model initializes.</li>
            <li>Large libraries can take several minutes to complete.</li>
          </ul>
        </div>
        <label className="modal-check">
          <input
            type="checkbox"
            checked={confirmed}
            onChange={(e) => onConfirmedChange(e.target.checked)}
          />
          <span>I understand that existing indexed entries will be reset.</span>
        </label>
        <div className="modal-actions">
          <button ref={cancelRef} type="button" className="btn" onClick={onCancel}>
            Cancel
          </button>
          <button
            type="button"
            className="btn btn-danger-soft"
            onClick={onConfirm}
            disabled={!confirmed}
          >
            Reset and start full scan
          </button>
        </div>
      </div>
    </div>
  )
}

export interface FullRescanModalController {
  open: boolean
  confirmed: boolean
  show: () => void
  close: () => void
  confirm: () => void
  setConfirmed: (v: boolean) => void
  cancelRef: React.RefObject<HTMLButtonElement | null>
  dialogRef: React.RefObject<HTMLDivElement | null>
  /** Element to restore focus to when the modal closes (or null to skip). */
  returnFocusRef: React.RefObject<HTMLElement | null>
}

interface Props {
  stats: Stats | null
  statsErrorKind: StatsErrorKind | null
  scanActive: boolean
  scanning: boolean
  paused: boolean
  page: AppPage
  onPageChange: (page: AppPage) => void
  refreshStats: () => void
  controlScan: (action: 'pause' | 'resume') => Promise<void>
  fullRescan: FullRescanModalController
  fullRescanNoticeActive: boolean
  /** Triggered when the topbar shows "Session expired" — typically calls useAuth().logout(). */
  onSessionExpired: () => void
  children: ReactNode
}

export default function PageShell({
  stats,
  statsErrorKind,
  scanActive,
  scanning,
  paused,
  page,
  onPageChange,
  refreshStats,
  controlScan,
  fullRescan,
  fullRescanNoticeActive,
  onSessionExpired,
  children,
}: Props) {
  const { state, logout } = useAuth()

  // Modal lifecycle — Esc to close, Tab focus trap, body scroll lock, focus
  // management on open/close. Refs are owned here so App doesn't need to
  // re-implement the same trap. The caller (App) is responsible for capturing
  // the trigger element into fullRescan.returnFocusRef BEFORE invoking
  // fullRescan.show() so focus restoration targets the right button.
  useEffect(() => {
    if (!fullRescan.open) return

    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    fullRescan.cancelRef.current?.focus()

    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault()
        fullRescan.close()
        return
      }
      if (e.key !== 'Tab') return

      const focusables = fullRescan.dialogRef.current?.querySelectorAll<HTMLElement>(
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
      const returnTo = fullRescan.returnFocusRef.current
      // Defer to next tick so we don't race with React's commit phase.
      window.setTimeout(() => {
        returnTo?.focus()
      }, 0)
    }
  }, [fullRescan])

  const onLogout = useCallback(() => {
    void logout()
  }, [logout])

  return (
    <div className="app">
      <FullRescanModal
        open={fullRescan.open}
        confirmed={fullRescan.confirmed}
        onConfirmedChange={fullRescan.setConfirmed}
        onCancel={fullRescan.close}
        onConfirm={fullRescan.confirm}
        cancelRef={fullRescan.cancelRef}
        dialogRef={fullRescan.dialogRef}
      />
      <a href="#main-content" className="skip-link">Skip to content</a>
      <header className="topbar">
        <div className="topbar-row">
          <button
            type="button"
            className="topbar-brand"
            onClick={() => onPageChange('search')}
            aria-label="Go to home"
          >
            <div>
              <h1>MetaTrace</h1>
              <div className="topbar-subtitle muted">Reverse image search</div>
            </div>
          </button>
          <div className="topbar-stats">
            {stats && scanActive ? (
              <>
                <span className={`pill ${scanActive ? 'pill-busy' : ''}`}>
                  {scanning ? 'scanning…' : 'paused'}
                </span>
                {scanActive && stats.last_report && (
                  <span className="pill pill-speed" title="Scan and embedding speed">
                    {fmtRate(getReportRates(stats.last_report).scansPerMin)} scans/min · {fmtRate(getReportRates(stats.last_report).embedsPerMin)} emb/min
                  </span>
                )}
                {scanning && (
                  <button className="btn btn-sm" onClick={() => void controlScan('pause')}>
                    Pause
                  </button>
                )}
                {paused && (
                  <button className="btn btn-sm" onClick={() => void controlScan('resume')}>
                    Resume
                  </button>
                )}
              </>
            ) : (
              statsErrorKind === 'network' ? (
                <span className="muted">
                  Cannot reach server
                  <button
                    className="btn btn-sm"
                    onClick={() => refreshStats()}
                    aria-label="Retry server connection"
                  >
                    Retry
                  </button>
                </span>
              ) : statsErrorKind === 'server' ? (
                <span className="muted">
                  Server temporarily unavailable
                  <button
                    className="btn btn-sm"
                    onClick={() => refreshStats()}
                    aria-label="Retry request"
                  >
                    Retry
                  </button>
                </span>
              ) : statsErrorKind === 'auth' ? (
                <span className="muted">
                  Session expired
                  <button
                    className="btn btn-sm"
                    onClick={onSessionExpired}
                    aria-label="Sign in again"
                  >
                    Sign in
                  </button>
                </span>
              ) : null
            )}
          </div>
          <nav className="topbar-nav-shell" aria-label="Main pages">
            <div className="topbar-nav">
              <button
                type="button"
                className={`nav-btn ${page === 'search' ? 'nav-btn-active' : ''}`}
                onClick={() => onPageChange('search')}
                aria-current={page === 'search' ? 'page' : undefined}
              >
                <NavIcon page="search" />
                <span className="nav-btn-label">Search</span>
              </button>
              <button
                type="button"
                className={`nav-btn ${page === 'browse' ? 'nav-btn-active' : ''}`}
                onClick={() => onPageChange('browse')}
                aria-current={page === 'browse' ? 'page' : undefined}
              >
                <NavIcon page="browse" />
                <span className="nav-btn-label">Browse</span>
              </button>
              <button
                type="button"
                className={`nav-btn ${page === 'settings' ? 'nav-btn-active' : ''}`}
                onClick={() => onPageChange('settings')}
                aria-current={page === 'settings' ? 'page' : undefined}
              >
                <NavIcon page="settings" />
                <span className="nav-btn-label">Settings</span>
              </button>
              {state.status === 'authenticated' && (
                <button
                  type="button"
                  className="nav-btn logout-btn"
                  onClick={onLogout}
                  aria-label="Log out"
                  title={state.user ? `Log out (${state.user.username})` : 'Log out'}
                >
                  <LogoutIcon className="nav-btn-icon" />
                  <span className="nav-btn-label">Log out</span>
                </button>
              )}
            </div>
          </nav>
        </div>
      </header>

      {stats?.last_report && <ScanReportLine report={stats.last_report} state={stats.state} />}
      {fullRescanNoticeActive && (
        <div className="global-notice-wrap">
          <div className="warning-box" role="status" aria-live="polite">
            <div className="warning-box-title">Initializing model and rebuilding index…</div>
            <div>
              Existing indexed entries are being reset and replaced. Search and browse results may be incomplete until the full scan finishes.
            </div>
          </div>
        </div>
      )}

      {children}

    </div>
  )
}