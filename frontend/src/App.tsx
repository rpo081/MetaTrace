import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react'
import type {
  AppPage,
} from './types'
import { ApiError, pauseRescan, resumeRescan, triggerRescan } from './api'
import { AuthProvider, useAuth } from './features/auth/AuthContext'
import { RequireAuth } from './features/auth/RequireAuth'
import { ForceChangePasswordModal } from './features/auth/ForceChangePasswordModal'
import { useStatsPolling } from './hooks/useStatsPolling'
import BrowseView from './components/BrowseView'
import PageShell from './components/PageShell'
import SearchView from './features/search/SearchView'
import SettingsView from './features/settings/SettingsView'

const RESCAN_STARTED_NOTICE = 'Rescan started.'

export default function App() {
  return (
    <AuthProvider>
      <RequireAuth>
        <AppContent />
      </RequireAuth>
    </AuthProvider>
  )
}

function AppContent() {
  const { state, logout } = useAuth()
  const [page, setPage] = useState<AppPage>('search')
  const { stats, delta, statsErrorKind, refreshStats } = useStatsPolling()

  const role = state.user?.role
  const canRescan = role === 'admin' || role === 'editor'
  const canFullScan = role === 'admin'
  const canSeeDelta = canRescan

  const scanning = stats?.state === 'scanning'
  const paused = stats?.state === 'paused'
  const scanActive = scanning || paused
  const scanStatusLabel = scanning ? 'Scanning' : paused ? 'Paused' : 'Idle'

  // App-level transient notice + error slots. Rendered inside SearchView's
  // sidebar because that's where the user already focuses after a scan/rescan.
  const [notice, setNotice] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  // Full-rescan modal controller (state + refs + lifecycle callbacks).
  const [showFullRescanModal, setShowFullRescanModal] = useState(false)
  const [fullRescanConfirmed, setFullRescanConfirmed] = useState(false)
  const [fullRescanNoticeActive, setFullRescanNoticeActive] = useState(false)
  const fullRescanCancelRef = useRef<HTMLButtonElement | null>(null)
  const fullRescanDialogRef = useRef<HTMLDivElement | null>(null)
  const fullRescanReturnFocusRef = useRef<HTMLElement | null>(null)

  // A finished scan retires the transient "Rescan started." notice.
  const wasScanningRef = useRef(false)
  useEffect(() => {
    const isScanning = stats?.state === 'scanning'
    const wasScanning = wasScanningRef.current
    wasScanningRef.current = isScanning
    if (wasScanning && !isScanning) {
      setNotice((n) => (n === RESCAN_STARTED_NOTICE ? null : n))
      setFullRescanNoticeActive(false)
    }
  }, [stats?.state])

  const rescan = useCallback(
    async (rebuild: boolean, useDelta = true): Promise<boolean> => {
      try {
        await triggerRescan(rebuild, useDelta)
        if (rebuild) {
          setNotice(null)
          setFullRescanNoticeActive(true)
        } else {
          setFullRescanNoticeActive(false)
          setNotice(RESCAN_STARTED_NOTICE)
        }
        refreshStats() // flip to "scanning" immediately, don't wait for the 10 s tick
        return true
      } catch (e) {
        setFullRescanNoticeActive(false)
        if (e instanceof ApiError && e.status === 409) {
          setNotice('A scan is already running.') // not an error
        } else {
          setNotice(null)
          setError(e instanceof Error ? e.message : String(e))
        }
        return false
      }
    },
    [refreshStats],
  )

  const openFullRescanModal = useCallback(() => {
    fullRescanReturnFocusRef.current = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null
    setFullRescanConfirmed(false)
    setShowFullRescanModal(true)
  }, [])

  const closeFullRescanModal = useCallback(() => {
    setShowFullRescanModal(false)
    setFullRescanConfirmed(false)
  }, [])

  const confirmFullRescan = useCallback(async () => {
    const started = await rescan(true, false)
    if (started) closeFullRescanModal()
  }, [closeFullRescanModal, rescan])

  const controlScan = useCallback(
    async (action: 'pause' | 'resume') => {
      try {
        if (action === 'pause') {
          await pauseRescan()
          setNotice('Scan pausing…')
        } else {
          await resumeRescan()
          setNotice('Scan resumed.')
        }
        refreshStats()
      } catch (e) {
        setNotice(null)
        setError(e instanceof Error ? e.message : String(e))
        refreshStats()
      }
    },
    [refreshStats],
  )

  // When polling determines the refresh cookie is also gone, the topbar
  // shows "Session expired" with a "Sign in" button that triggers logout —
  // useAuth flips to `unauthenticated` and RequireAuth re-renders LoginPage.
  const onSessionExpired = useCallback(() => {
    void logout()
  }, [logout])

  const fullRescanController = useMemo(
    () => ({
      open: showFullRescanModal,
      confirmed: fullRescanConfirmed,
      show: openFullRescanModal,
      close: closeFullRescanModal,
      confirm: confirmFullRescan,
      setConfirmed: setFullRescanConfirmed,
      cancelRef: fullRescanCancelRef,
      dialogRef: fullRescanDialogRef,
      returnFocusRef: fullRescanReturnFocusRef,
    }),
    [
      showFullRescanModal,
      fullRescanConfirmed,
      openFullRescanModal,
      closeFullRescanModal,
      confirmFullRescan,
    ],
  )

  const renderPage = () => {
    if (page === 'search') {
      return (
        <SearchView
          stats={stats}
          refreshStats={refreshStats}
          notice={notice}
          error={error}
        />
      )
    }
    if (page === 'browse') {
      return <BrowseView />
    }
    return (
      <SettingsView
        stats={stats}
        delta={delta}
        scanActive={scanActive}
        scanStatusLabel={scanStatusLabel}
        canRescan={canRescan}
        canFullScan={canFullScan}
        canSeeDelta={canSeeDelta}
        refreshStats={refreshStats}
        rescan={rescan}
        openFullRescanModal={openFullRescanModal}
        onGlobalNotice={setNotice}
      />
    )
  }

  return (
    <PageShell
      stats={stats}
      statsErrorKind={statsErrorKind}
      scanActive={scanActive}
      scanning={scanning}
      paused={paused}
      page={page}
      onPageChange={setPage}
      refreshStats={refreshStats}
      controlScan={controlScan}
      fullRescan={fullRescanController}
      fullRescanNoticeActive={fullRescanNoticeActive}
      onSessionExpired={onSessionExpired}
    >
      {renderPage()}
      <ForceChangePasswordModal />
    </PageShell>
  )
}