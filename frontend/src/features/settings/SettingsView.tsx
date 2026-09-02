/** SettingsView — read-only runtime info + library maintenance (rescan) +
 *  scan activity report + account (change password) + admin user mgmt.
 *  Rescan + delta info lives here because the user clicks "Rescan changes"
 *  from the settings page. Full-rescan modal lifecycle (refs, focus trap,
 *  esc) stays in PageShell — we only call openFullRescanModal().
 */
import { useCallback, useEffect, useState } from 'react'
import type { RescanDeltaResponse, Stats } from '../../types'
import ScanReportLine, { getReportRates } from '../../components/ScanReportLine'
import { ChangePasswordModal } from '../auth/ChangePasswordModal'
import { UserManagementSection } from '../auth/UserManagementSection'
import { useAuth } from '../auth/AuthContext'
import { getDisplayPathPrefix, updateDisplayPathPrefix } from '../../api'

function fmtRate(val: number | undefined): string {
  if (val == null || val <= 0) return '0'
  if (val >= 100) return `${Math.round(val)}`
  return `${Math.round(val * 10) / 10}`
}

function DeltaInfo({
  delta,
  onRescanWithDelta,
  onOpenFullRescan,
  loading,
  title = 'Changes since last scan:',
  canFullScan = true,
}: {
  delta: RescanDeltaResponse | null
  onRescanWithDelta: () => void
  onOpenFullRescan: () => void
  loading: boolean
  title?: string
  canFullScan?: boolean
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
        {canFullScan && (
          <button
            className="btn btn-sm btn-danger-soft"
            onClick={onOpenFullRescan}
            disabled={loading}
            title="Reset indexed data and start a full scan"
          >
            Reset + full scan
          </button>
        )}
      </div>
    </div>
  )
}

interface Props {
  stats: Stats | null
  delta: RescanDeltaResponse | null
  scanActive: boolean
  scanStatusLabel: 'Scanning' | 'Paused' | 'Idle'
  canRescan: boolean
  canFullScan: boolean
  canSeeDelta: boolean
  rescan: (rebuild: boolean, useDelta?: boolean) => Promise<boolean>
  openFullRescanModal: () => void
  /** Bubble a transient global notice (e.g. "Password updated") up to the
   *  App-level notice slot rendered in the search sidebar. */
  onGlobalNotice?: (msg: string | null) => void
}

export default function SettingsView({
  stats,
  delta,
  scanActive,
  scanStatusLabel,
  canRescan,
  canFullScan,
  canSeeDelta,
  rescan,
  openFullRescanModal,
  onGlobalNotice,
}: Props) {
  const { state } = useAuth()
  const [showPw, setShowPw] = useState(false)
  const [pathPrefix, setPathPrefix] = useState('')
  const [pathPrefixLoading, setPathPrefixLoading] = useState(true)
  const [pathPrefixSaving, setPathPrefixSaving] = useState(false)
  const isAdmin = state.user?.role === 'admin'

  useEffect(() => {
    const controller = new AbortController()
    void getDisplayPathPrefix(controller.signal)
      .then(({ prefix }) => setPathPrefix(prefix))
      .catch(() => {})
      .finally(() => setPathPrefixLoading(false))
    return () => controller.abort()
  }, [])

  const closePw = useCallback(() => {
    setShowPw(false)
  }, [])

  const handlePwSuccess = useCallback(() => {
    setShowPw(false)
    onGlobalNotice?.('Password updated. Other sessions were signed out.')
  }, [onGlobalNotice])

  const savePathPrefix = useCallback(async () => {
    setPathPrefixSaving(true)
    try {
      await updateDisplayPathPrefix(pathPrefix)
      window.location.reload()
    } catch (e) {
      onGlobalNotice?.(e instanceof Error ? e.message : String(e))
    } finally {
      setPathPrefixSaving(false)
    }
  }, [onGlobalNotice, pathPrefix])

  return (
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
            <h2>Server Path Prefix</h2>
            <p className="muted">Shown before each relative image path without changing stored paths.</p>
          </div>
          <div className="settings-result">
            <label className="control" htmlFor="display-path-prefix">
              <span>Prefix</span>
              <input
                id="display-path-prefix"
                type="text"
                className="text-input mono"
                value={pathPrefix}
                onChange={(e) => setPathPrefix(e.target.value)}
                placeholder="\\\\server\\share"
                readOnly={!isAdmin}
                disabled={pathPrefixLoading || pathPrefixSaving}
              />
            </label>
            {isAdmin ? (
              <div className="settings-actions">
                <button
                  type="button"
                  className="btn btn-primary"
                  onClick={() => void savePathPrefix()}
                  disabled={pathPrefixLoading || pathPrefixSaving}
                >
                  {pathPrefixSaving ? 'Saving…' : 'Save prefix'}
                </button>
              </div>
            ) : (
              <div className="muted">Only administrators can change this setting.</div>
            )}
          </div>
        </section>

        <section className="settings-card">
          <div className="settings-header">
            <h2>Library Maintenance</h2>
            <p className="muted">Manage indexing and rebuild operations for the local image library.</p>
          </div>
          {canSeeDelta ? (
            delta?.status === 'ok' && delta.summary ? (
              <DeltaInfo
                delta={delta}
                onRescanWithDelta={() => void rescan(false, true)}
                onOpenFullRescan={openFullRescanModal}
                loading={scanActive}
                title="Pending changes"
                canFullScan={canFullScan}
              />
            ) : (
              <div className="delta-info" role="status">
                <div className="delta-info-title">Pending changes</div>
                <div className="muted">No change summary is currently available.</div>
              </div>
            )
          ) : (
            <div className="delta-info" role="status">
              <div className="delta-info-title">Pending changes</div>
              <div className="muted">Library updates are managed by editors and admins.</div>
            </div>
          )}
          {canSeeDelta && (
            <div className="scan-actions maintenance-actions">
              {canRescan && (
                <button
                  className="btn"
                  onClick={() => void rescan(false, true)}
                  disabled={scanActive}
                  title={scanActive ? 'A scan is already running' : 'Scan changed files when delta data is available'}
                >
                  Rescan changes
                </button>
              )}
              {canFullScan && (
                <button
                  className="btn btn-danger-soft"
                  onClick={openFullRescanModal}
                  disabled={scanActive}
                  title="Reset indexed data and start a full scan"
                >
                  Reset + full scan
                </button>
              )}
            </div>
          )}
          {canSeeDelta && (
            <div className="maintenance-note muted">
              Full scan replaces existing indexed data and may temporarily reduce search and browse completeness.
            </div>
          )}
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

        <section className="settings-card">
          <div className="settings-header">
            <h2>Account</h2>
            <p className="muted">Password &amp; Security</p>
          </div>
          <div className="settings-result">
            <div className="muted">Signed in as {state.user?.username} — {state.user?.role}</div>
            <button type="button" className="btn" onClick={() => setShowPw(true)}>
              Change password
            </button>
          </div>
        </section>

        {state.user?.role === 'admin' && (
          <UserManagementSection currentUserId={state.user.id} />
        )}
      </div>

      {showPw && (
        <ChangePasswordModal
          mode="self"
          dismissible={true}
          onCancel={closePw}
          onSuccess={handlePwSuccess}
        />
      )}
    </main>
  )
}