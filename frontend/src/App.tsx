import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError, getStats, searchImage, triggerRescan, getRescanDelta, pauseRescan, resumeRescan } from './api'
import Dropzone from './components/Dropzone'
import DetailPanel from './components/DetailPanel'
import ResultGrid from './components/ResultGrid'
import type { ScanReport, SearchResponse, SearchResult, Stats, RescanDeltaResponse } from './types'

const IDLE_REFRESH_MS = 10_000
const ACTIVE_SCAN_REFRESH_MS = 1_000

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

function DeltaInfo({
  delta,
  onRescanWithDelta,
  onRescanFull,
  loading,
}: {
  delta: RescanDeltaResponse | null
  onRescanWithDelta: () => void
  onRescanFull: () => void
  loading: boolean
}) {
  if (!delta || delta.status === 'no_delta') {
    return null
  }
  const { summary } = delta
  if (!summary) return null

  const totalChanges = summary.total_changes
  return (
    <div className="delta-info" role="status">
      <div className="delta-info-title">Changes since last scan:</div>
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
        <button className="btn btn-sm" onClick={onRescanFull} disabled={loading} title="Full rescan">
          Full
        </button>
      </div>
    </div>
  )
}

export default function App() {
  const [stats, setStats] = useState<Stats | null>(null)
  const [delta, setDelta] = useState<RescanDeltaResponse | null>(null)
  const [file, setFile] = useState<File | null>(null)
  const [previewUrl, setPreviewUrl] = useState<string | null>(null)
  const [k, setK] = useState(5)
  const [minScore, setMinScore] = useState(0.0)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [response, setResponse] = useState<SearchResponse | null>(null)
  const [selected, setSelected] = useState<SearchResult | null>(null)

  const abortRef = useRef<AbortController | null>(null)
  const previewUrlRef = useRef<string | null>(null)

  const refreshStats = useCallback(() => {
    getStats().then(setStats).catch(() => {})
    getRescanDelta().then(setDelta).catch(() => {})
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
  // A finished scan retires the transient "Rescan started." notice.
  const wasScanningRef = useRef(false)
  useEffect(() => {
    const scanning = stats?.state === 'scanning'
    const wasScanning = wasScanningRef.current
    wasScanningRef.current = scanning
    if (wasScanning && !scanning) {
      setNotice((n) => (n === 'Rescan started.' ? null : n))
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

  const runSearch = useCallback(async () => {
    if (!file) return
    abortRef.current?.abort() // kill any in-flight search (stale-response race)
    const controller = new AbortController()
    abortRef.current = controller
    setLoading(true)
    setError(null)
    setNotice(null)
    setSelected(null)
    try {
      const res = await searchImage(file, k, minScore, controller.signal)
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
  }, [file, k, minScore, refreshStats])

  const rescan = useCallback(
    async (rebuild: boolean, useDelta: boolean = true) => {
      try {
        await triggerRescan(rebuild, useDelta)
        setError(null)
        setNotice('Rescan started.')
        refreshStats() // flip to "scanning" immediately, don't wait for the 10 s tick
      } catch (e) {
        if (e instanceof ApiError && e.status === 409) {
          setNotice('A scan is already running.') // not an error
        } else {
          setNotice(null)
          setError(e instanceof Error ? e.message : String(e))
        }
      }
      setTimeout(refreshStats, 500)
    },
    [refreshStats],
  )

  const scanning = stats?.state === 'scanning'
  const paused = stats?.state === 'paused'
  const scanActive = scanning || paused
  const inventoryLabel = inventorySourceLabel(stats?.inventory_source)
  const indexedLabel = stats?.snapshot_image_count == null
    ? `${stats?.indexed} indexed`
    : `${stats.indexed} / ${stats.snapshot_image_count} indexed`

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
      <header className="topbar">
        <h1>MetaTrace</h1>
        <div className="topbar-stats">
          {stats ? (
            <>
              <span className={`pill ${scanActive ? 'pill-busy' : ''}`}>
                {scanning ? 'scanning…' : paused ? 'paused' : indexedLabel}
              </span>
              {scanActive && stats.last_report && (
                <span className="pill pill-speed" title="Scan and embedding speed">
                  {fmtRate(getReportRates(stats.last_report).scansPerMin)} scans/min · {fmtRate(getReportRates(stats.last_report).embedsPerMin)} emb/min
                </span>
              )}
              <span className="muted">{stats.model}</span>
              {scanActive && inventoryLabel && <span className="muted">{inventoryLabel}</span>}
              {!stats.exiftool && (
                <span
                  className="pill pill-warn"
                  title="exiftool is not installed on the server, so embedded XMP tags cannot be shown. Install it (e.g. brew install exiftool) and trigger a rescan."
                >
                  exiftool missing · no XMP
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
              {!scanActive && delta?.status === 'ok' && delta.summary && (
                <DeltaInfo
                  delta={delta}
                  onRescanWithDelta={() => rescan(false, true)}
                  onRescanFull={() => rescan(false, false)}
                  loading={scanning}
                />
              )}
              {!scanActive && (delta?.status === 'no_delta' || !delta) && (
                <button
                  className="btn"
                  onClick={() => rescan(false, true)}
                  disabled={scanActive}
                  title={scanActive ? 'A scan is already running' : 'Scan (delta-optimized if available)'}
                >
                  Rescan
                </button>
              )}
            </>
          ) : (
            <span className="muted">connecting…</span>
          )}
        </div>
      </header>

      {stats?.last_report && <ScanReportLine report={stats.last_report} state={stats.state} />}

      <main className="layout">
        <section className="sidebar">
          <Dropzone previewUrl={previewUrl} onFile={onFile} disabled={loading} />

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

          <button className="btn btn-primary" onClick={runSearch} disabled={!file || loading}>
            {loading ? 'Searching…' : 'Search'}
          </button>

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
              <p className="result-meta muted">
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
                    onSelect={setSelected}
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
    </div>
  )
}
