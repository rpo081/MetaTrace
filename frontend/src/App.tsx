import { useCallback, useEffect, useRef, useState } from 'react'
import {
  ApiError,
  getStats,
  searchImage,
  triggerRescan,
  getRescanDelta,
  pauseRescan,
  resumeRescan,
} from './api'
import Dropzone from './components/Dropzone'
import DetailPanel from './components/DetailPanel'
import ResultGrid from './components/ResultGrid'
import BrowseView from './components/BrowseView'
import { SearchIcon, GridIcon, GearIcon, CloseIcon } from './components/Icon'
import type {
  AppPage,
  ScanReport,
  SearchCombineMode,
  SearchResponse,
  SearchResult,
  Stats,
  RescanDeltaResponse,
} from './types'

const IDLE_REFRESH_MS = 10_000
const ACTIVE_SCAN_REFRESH_MS = 2_000
const STATS_TIMEOUT_MS = 10_000
const SEARCH_TIMEOUT_MS = 30_000
const RESCAN_STARTED_NOTICE = 'Rescan started.'

function timeoutError(message: string): Error {
  const error = new Error(message)
  error.name = 'AbortError'
  return error
}

async function withTimeout<T>(
  run: (signal: AbortSignal) => Promise<T>,
  timeoutMs: number,
  message: string,
  parentSignal?: AbortSignal,
): Promise<T> {
  const controller = new AbortController()
  const timer = window.setTimeout(() => controller.abort(), timeoutMs)
  const onAbort = () => controller.abort()
  parentSignal?.addEventListener('abort', onAbort)

  try {
    const value = await run(controller.signal)
    if (controller.signal.aborted) {
      throw timeoutError(message)
    }
    return value
  } catch (error) {
    if (controller.signal.aborted && !parentSignal?.aborted) {
      throw timeoutError(message)
    }
    throw error
  } finally {
    window.clearTimeout(timer)
    parentSignal?.removeEventListener('abort', onAbort)
  }
}

function fmtDuration(sec: number): string {
  return sec >= 10 ? `${Math.round(sec)}s` : `${Math.round(sec * 10) / 10}s`
}

function fmtRate(val: number | undefined): string {
  if (val == null || val <= 0) return '0'
  if (val >= 100) return `${Math.round(val)}`
  return `${Math.round(val * 10) / 10}`
}

function getReportRates(report: ScanReport) {
  const indexed = report.added + report.updated
  const elapsed = report.elapsed_sec ?? report.duration_sec ?? 0
  const scansPerMin = report.scans_per_min ?? (elapsed > 0 ? (report.processed / elapsed) * 60 : 0)
  const embedsPerMin = report.embeddings_per_min ?? (elapsed > 0 ? (indexed / elapsed) * 60 : 0)
  return { indexed, elapsed, scansPerMin, embedsPerMin }
}

function ScanReportLine({ report, state }: { report: ScanReport; state: string | undefined }) {
  const { indexed, scansPerMin, embedsPerMin } = getReportRates(report)
  return (
    <div className="scan-report" role="status">
      <span className="scan-report-label">
        {state === 'paused'
          ? `Paused scan (${report.trigger})`
          : state === 'scanning'
            ? `Scanning (${report.trigger})`
            : `Last scan (${report.trigger})`}
      </span>
      <span className="mono">
        {state === 'scanning' || state === 'paused' ? (
          <>
            {report.processed} / {report.seen} scanned ({fmtRate(scansPerMin)}/min) · {indexed} embedded ({fmtRate(embedsPerMin)}/min) · {report.failed} failed
          </>
        ) : (
          <>
            +{report.added} added · {report.updated} updated · −{report.removed} removed ·{' '}
            {report.failed} failed · {fmtDuration(report.duration_sec)} ({fmtRate(scansPerMin)} scans/min · {fmtRate(embedsPerMin)} emb/min)
          </>
        )}
      </span>
    </div>
  )
}

function inventorySourceLabel(source: Stats['inventory_source']): string | null {
  if (source === 'snapshot') return 'Inventory: snapshot'
  if (source === 'walk') return 'Inventory: filesystem walk'
  return null
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

function DeltaInfo({
  delta,
  onRescanWithDelta,
  onOpenFullRescan,
  loading,
  title = 'Changes since last scan:',
}: {
  delta: RescanDeltaResponse | null
  onRescanWithDelta: () => void
  onOpenFullRescan: () => void
  loading: boolean
  title?: string
}) {
  if (!delta || delta.status === 'no_delta') {
    return null
  }
  const { summary } = delta
  if (!summary) return null

  const totalChanges = summary.total_changes
  return (
    <div className="delta-info" role="status">
      <div className="delta-info-title">{title}</div>
      <div className="delta-info-details">
        {summary.created_count > 0 && (
          <span className="delta-created">+{summary.created_count} new</span>
        )}
        {summary.modified_count > 0 && (
          <span className="delta-modified">~{summary.modified_count} modified</span>
        )}
        {summary.deleted_count > 0 && (
          <span className="delta-deleted">−{summary.deleted_count} deleted</span>
        )}
      </div>
      <div className="delta-info-actions">
        <button
          className="btn btn-sm"
          onClick={onRescanWithDelta}
          disabled={loading || totalChanges === 0}
          title={totalChanges === 0 ? 'No changes to scan' : 'Scan only changed files'}
        >
          Rescan ({totalChanges})
        </button>
        <button
          className="btn btn-sm btn-danger-soft"
          onClick={onOpenFullRescan}
          disabled={loading}
          title="Reset indexed data and start a full scan"
        >
          Reset + full scan
        </button>
      </div>
    </div>
  )
}

export default function App() {
  const [page, setPage] = useState<AppPage>('search')
  const [stats, setStats] = useState<Stats | null>(null)
  const [delta, setDelta] = useState<RescanDeltaResponse | null>(null)
  const [file, setFile] = useState<File | null>(null)
  const [previewUrl, setPreviewUrl] = useState<string | null>(null)
  const [textQuery, setTextQuery] = useState('')
  const [combineMode, setCombineMode] = useState<SearchCombineMode>('and')
  const [k, setK] = useState(5)
  const [minScore, setMinScore] = useState(0.0)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [response, setResponse] = useState<SearchResponse | null>(null)
  const [selected, setSelected] = useState<SearchResult | null>(null)
  const [statsError, setStatsError] = useState(false)
  const [statsRetryCount, setStatsRetryCount] = useState(0)
  const [showFullRescanModal, setShowFullRescanModal] = useState(false)
  const [fullRescanConfirmed, setFullRescanConfirmed] = useState(false)
  const [fullRescanNoticeActive, setFullRescanNoticeActive] = useState(false)

  const abortRef = useRef<AbortController | null>(null)
  const previewUrlRef = useRef<string | null>(null)
  const fullRescanCancelRef = useRef<HTMLButtonElement | null>(null)
  const fullRescanDialogRef = useRef<HTMLDivElement | null>(null)
  const fullRescanReturnFocusRef = useRef<HTMLElement | null>(null)

  const refreshStats = useCallback(() => {
    withTimeout(getStats, STATS_TIMEOUT_MS, 'Cannot reach server. Please retry.')
      .then((s) => {
        setStats(s)
        setStatsError(false)
        setStatsRetryCount(0)
      })
      .catch(() => {
        setStatsError(true)
        setStatsRetryCount((count) => count + 1)
      })
    withTimeout(getRescanDelta, STATS_TIMEOUT_MS, 'Failed to load scan changes.')
      .then(setDelta)
      .catch(() => {})
  }, [])

  useEffect(refreshStats, [refreshStats])
  useEffect(() => {
    const t = setInterval(refreshStats, IDLE_REFRESH_MS)
    return () => clearInterval(t)
  }, [refreshStats])
  // Poll more frequently during scans, but keep it coarse enough to avoid
  // turning status updates into a steady stream of access-log noise.
  useEffect(() => {
    if (stats?.state !== 'scanning' && stats?.state !== 'paused') return
    const t = setInterval(refreshStats, ACTIVE_SCAN_REFRESH_MS)
    return () => clearInterval(t)
  }, [stats?.state, refreshStats])
  useEffect(() => {
    if (!statsError || statsRetryCount >= 3) return
    const delay = Math.min(2 ** (statsRetryCount - 1) * 2_000, 8_000)
    const t = window.setTimeout(refreshStats, delay)
    return () => window.clearTimeout(t)
  }, [statsError, statsRetryCount, refreshStats])
  // A finished scan retires the transient "Rescan started." notice.
  const wasScanningRef = useRef(false)
  useEffect(() => {
    const scanning = stats?.state === 'scanning'
    const wasScanning = wasScanningRef.current
    wasScanningRef.current = scanning
    if (wasScanning && !scanning) {
      setNotice((n) => (n === RESCAN_STARTED_NOTICE ? null : n))
      setFullRescanNoticeActive(false)
    }
  }, [stats?.state])

  // Track latest values for unmount cleanup without re-registering the effect.
  useEffect(() => {
    previewUrlRef.current = previewUrl
  }, [previewUrl])
  useEffect(
    () => () => {
      abortRef.current?.abort()
      if (previewUrlRef.current) URL.revokeObjectURL(previewUrlRef.current)
    },
    [],
  )

  useEffect(() => {
    if (!showFullRescanModal) return

    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    fullRescanCancelRef.current?.focus()

    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault()
        setShowFullRescanModal(false)
        setFullRescanConfirmed(false)
        return
      }
      if (e.key !== 'Tab') return

      const focusables = fullRescanDialogRef.current?.querySelectorAll<HTMLElement>(
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
      fullRescanReturnFocusRef.current?.focus()
      fullRescanReturnFocusRef.current = null
    }
  }, [showFullRescanModal])

  // Close detail panel on Escape
  useEffect(() => {
    if (!selected) return
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setSelected(null)
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [selected])

  const onFile = useCallback(
    (f: File) => {
      // Client-side size pre-check; backend enforces the same limit (413).
      const maxMb = stats?.max_upload_mb
      if (maxMb != null && f.size > maxMb * 1024 * 1024) {
        setError(`"${f.name}" exceeds the ${maxMb} MiB upload limit.`)
        return
      }
      setError(null)
      setNotice(null)
      setFile(f)
      if (previewUrl) URL.revokeObjectURL(previewUrl) // blob URL hygiene
      setPreviewUrl(URL.createObjectURL(f))
    },
    [stats?.max_upload_mb, previewUrl],
  )

  const onClearFile = useCallback(() => {
    setFile(null)
    if (previewUrl) {
      URL.revokeObjectURL(previewUrl)
      setPreviewUrl(null)
    }
  }, [previewUrl])

  const runSearch = useCallback(async () => {
    if (!file && !textQuery.trim()) return
    abortRef.current?.abort() // kill any in-flight search (stale-response race)
    const controller = new AbortController()
    abortRef.current = controller
    setLoading(true)
    setError(null)
    setNotice(null)
    setSelected(null)
    try {
      const res = await withTimeout(
        (signal) => searchImage(file, k, minScore, textQuery, combineMode, signal),
        SEARCH_TIMEOUT_MS,
        'Search is taking longer than expected. Please try again.',
        controller.signal,
      )
      setResponse(res)
    } catch (e) {
      if (controller.signal.aborted || (e instanceof DOMException && e.name === 'AbortError')) {
        return // superseded by a newer search — leave state alone
      }
      setError(e instanceof Error ? e.message : String(e))
      setResponse(null)
    } finally {
      if (abortRef.current === controller) {
        setLoading(false)
        refreshStats()
      }
    }
  }, [file, textQuery, combineMode, k, minScore, refreshStats])

  const rescan = useCallback(
    async (rebuild: boolean, useDelta: boolean = true): Promise<boolean> => {
      try {
        await triggerRescan(rebuild, useDelta)
        setError(null)
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
      window.setTimeout(refreshStats, 500)
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

  const scanning = stats?.state === 'scanning'
  const paused = stats?.state === 'paused'
  const scanActive = scanning || paused
  const indexedLabel = stats?.snapshot_image_count == null
    ? `${stats?.indexed} indexed`
    : `${stats.indexed} / ${stats.snapshot_image_count} indexed`
  const scanStatusLabel = scanning ? 'Scanning' : paused ? 'Paused' : 'Idle'

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
        setError(null)
        refreshStats()
      } catch (e) {
        setNotice(null)
        setError(e instanceof Error ? e.message : String(e))
      }
    },
    [refreshStats],
  )

  return (
    <div className="app">
      <FullRescanModal
        open={showFullRescanModal}
        confirmed={fullRescanConfirmed}
        onConfirmedChange={setFullRescanConfirmed}
        onCancel={closeFullRescanModal}
        onConfirm={confirmFullRescan}
        cancelRef={fullRescanCancelRef}
        dialogRef={fullRescanDialogRef}
      />
      <a href="#main-content" className="skip-link">Skip to content</a>
      <header className="topbar">
        <div className="topbar-row">
          <div className="topbar-brand">
            <div>
              <h1>MetaTrace</h1>
              <div className="topbar-subtitle muted">Reverse image search</div>
            </div>
          </div>
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
                  <button className="btn btn-sm" onClick={() => controlScan('pause')}>
                    Pause
                  </button>
                )}
                {paused && (
                  <button className="btn btn-sm" onClick={() => controlScan('resume')}>
                    Resume
                  </button>
                )}
              </>
            ) : (
              statsError ? (
                <span className="muted">
                  Cannot reach server
                  <button
                    className="btn btn-sm"
                    onClick={refreshStats}
                    aria-label="Retry server connection"
                  >
                    Retry
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
                onClick={() => setPage('search')}
                aria-current={page === 'search' ? 'page' : undefined}
              >
                <NavIcon page="search" />
                <span className="nav-btn-label">Search</span>
              </button>
              <button
                type="button"
                className={`nav-btn ${page === 'browse' ? 'nav-btn-active' : ''}`}
                onClick={() => setPage('browse')}
                aria-current={page === 'browse' ? 'page' : undefined}
              >
                <NavIcon page="browse" />
                <span className="nav-btn-label">Browse</span>
              </button>
              <button
                type="button"
                className={`nav-btn ${page === 'settings' ? 'nav-btn-active' : ''}`}
                onClick={() => setPage('settings')}
                aria-current={page === 'settings' ? 'page' : undefined}
              >
                <NavIcon page="settings" />
                <span className="nav-btn-label">Settings</span>
              </button>
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

      {page === 'search' ? (
      <main id="main-content" className="layout">
        <section className="sidebar">
          <Dropzone previewUrl={previewUrl} onFile={onFile} onClear={onClearFile} disabled={loading} />

          <div className="text-search-box">
            <label htmlFor="text-query-input" className="text-search-label">
              Text / Project / XMP Search
            </label>
            <div className="text-search-input-wrap">
              <input
                id="text-query-input"
                type="text"
                className="text-search-input"
                placeholder="Image name, project, XMP..."
                value={textQuery}
                onChange={(e) => setTextQuery(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') runSearch()
                }}
                disabled={loading}
              />
              {textQuery && (
                <button
                  type="button"
                  className="btn-clear-text"
                  onClick={() => setTextQuery('')}
                  title="Clear text search"
                  disabled={loading}
                  aria-label="Clear text search"
                >
                   <CloseIcon width="14" height="14" />
                 </button>
              )}
            </div>
            <div className="search-mode-toggle" role="group" aria-label="Combine image and text search" aria-describedby="combine-mode-help">
              <button
                type="button"
                className={`toggle-chip ${combineMode === 'and' ? 'toggle-chip-active' : ''}`}
                onClick={() => setCombineMode('and')}
                aria-pressed={combineMode === 'and'}
                disabled={loading}
              >
                AND
              </button>
              <button
                type="button"
                className={`toggle-chip ${combineMode === 'or' ? 'toggle-chip-active' : ''}`}
                onClick={() => setCombineMode('or')}
                aria-pressed={combineMode === 'or'}
                disabled={loading}
              >
                OR
              </button>
            </div>
            <div className="muted text-search-help" id="combine-mode-help">
              {combineMode === 'and'
                ? 'Image + text: only results that match visually AND match the name/XMP.'
                : 'Image + text: visual and textual results are combined.'}
            </div>
          </div>

          <div className="controls">
            <label className="control">
              <span>
                Results <b>{k}</b>
              </span>
              <input
                type="range"
                min={1}
                max={30}
                step={1}
                value={k}
                disabled={loading}
                onChange={(e) => setK(Number(e.target.value))}
              />
            </label>
            <label className="control">
              <span>
                Min score <b>{Math.round(minScore * 100)}%</b>
              </span>
              <input
                type="range"
                min={0}
                max={0.95}
                step={0.05}
                value={minScore}
                disabled={loading}
                onChange={(e) => setMinScore(Number(e.target.value))}
              />
            </label>
          </div>

          <div className="search-action-bar">
            <button
              className="btn btn-primary search-action-btn"
              onClick={runSearch}
              disabled={(!file && !textQuery.trim()) || loading}
            >
              {loading ? 'Searching…' : 'Search'}
            </button>
          </div>

          {error && (
            <div className="error-box" role="alert">
              {error}
            </div>
          )}
          {notice && (
            <div className="info-box" role="status">
              {notice}
            </div>
          )}
        </section>

        <section className="content">
          {loading && (
            <div className="busy-overlay" role="status" aria-live="polite">
              <span className="spinner" aria-hidden />
              Searching…
            </div>
          )}
          {response ? (
            <>
              <p className="result-meta muted" aria-live="polite">
                {response.results.length} of {response.total_indexed} indexed images
                {response.exact_match && ' · byte-identical match found'}
              </p>
              {response.results.length === 0 ? (
                <div className="info-box" role="status">
                  {response.total_indexed === 0
                    ? 'Index is empty — initial scan may be running. Try again once indexing completes.'
                    : 'No matches above the score threshold.'}
                </div>
              ) : (
                <div className={selected ? 'split' : ''}>
                  <ResultGrid
                    results={response.results}
                    selectedId={selected?.id ?? null}
                    onSelect={(r) => { if ('score' in r) setSelected(r as SearchResult) }}
                  />
                  {selected && <DetailPanel result={selected} onClose={() => setSelected(null)} />}
                </div>
              )}
            </>
          ) : (
            <div className="placeholder">
              <p>Upload a query image and hit Search.</p>
              <p className="muted">
                Matches include exact copies, resized variants and visually similar renderings.
              </p>
            </div>
          )}
        </section>
      </main>
      ) : page === 'browse' ? (
      <BrowseView />
      ) : (
      <main id="main-content" className="settings-view">
        <div className="settings-grid">
          <section className="settings-card">
            <div className="settings-header">
              <h2>Settings</h2>
              <p className="muted">Read-only runtime information for this instance.</p>
            </div>
            <div className="settings-result">
              <div className="settings-result-title">Runtime</div>
              <div className="summary-grid">
                <span className="summary-chip">Model: {stats?.model ?? 'unknown'}</span>
                <span className="summary-chip">Indexed: {stats?.indexed ?? 0}</span>
                <span className="summary-chip">Upload limit: {stats?.max_upload_mb ?? 'n/a'} MiB</span>
                <span className="summary-chip">ExifTool: {stats?.exiftool ? 'available' : 'missing'}</span>
              </div>
              {stats?.last_scan && <div className="summary-row muted">Last scan: {stats.last_scan}</div>}
            </div>
          </section>

          <section className="settings-card">
            <div className="settings-header">
              <h2>Library Maintenance</h2>
              <p className="muted">Manage indexing and rebuild operations for the local image library.</p>
            </div>
            {delta?.status === 'ok' && delta.summary ? (
              <DeltaInfo
                delta={delta}
                onRescanWithDelta={() => rescan(false, true)}
                onOpenFullRescan={openFullRescanModal}
                loading={scanActive}
                title="Pending changes"
              />
            ) : (
              <div className="delta-info" role="status">
                <div className="delta-info-title">Pending changes</div>
                <div className="muted">No change summary is currently available.</div>
              </div>
            )}
            <div className="scan-actions maintenance-actions">
              <button
                className="btn"
                onClick={() => rescan(false, true)}
                disabled={scanActive}
                title={scanActive ? 'A scan is already running' : 'Scan changed files when delta data is available'}
              >
                Rescan changes
              </button>
              <button
                className="btn btn-danger-soft"
                onClick={openFullRescanModal}
                disabled={scanActive}
                title="Reset indexed data and start a full scan"
              >
                Reset + full scan
              </button>
            </div>
            <div className="maintenance-note muted">
              Full scan replaces existing indexed data and may temporarily reduce search and browse completeness.
            </div>
          </section>

          <section className="settings-card">
            <div className="settings-header">
              <h2>Scan Activity</h2>
              <p className="muted">Current scan state and latest indexing report.</p>
            </div>
            <div className="summary-grid">
              <span className="summary-chip">State: {scanStatusLabel}</span>
              {stats?.last_report && (
                <span className="summary-chip">
                  Speed: {fmtRate(getReportRates(stats.last_report).scansPerMin)} scans/min
                </span>
              )}
              {stats?.last_report && (
                <span className="summary-chip">
                  Embeddings: {fmtRate(getReportRates(stats.last_report).embedsPerMin)} /min
                </span>
              )}
            </div>
            {stats?.last_report ? (
              <ScanReportLine report={stats.last_report} state={stats.state} />
            ) : (
              <div className="muted">No scan report is available yet.</div>
            )}
          </section>
        </div>
      </main>
      )}
    </div>
  )
}
