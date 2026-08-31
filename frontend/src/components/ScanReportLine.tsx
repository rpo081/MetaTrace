import type { ScanReport } from '../types'

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

export default function ScanReportLine({ report, state }: { report: ScanReport; state: string | undefined }) {
  const { indexed, scansPerMin, embedsPerMin } = getReportRates(report)
  return (
    <div className="scan-report" role="status">
      <span className="scan-report-label">
        {state === 'paused' ? `Paused scan (${report.trigger})` : state === 'scanning' ? `Scanning (${report.trigger})` : `Last scan (${report.trigger})`}
      </span>
      <span className="mono">
        {state === 'scanning' || state === 'paused' ? (
          <>
            {report.processed} / {report.seen} scanned ({fmtRate(scansPerMin)}/min) · {indexed} embedded ({fmtRate(embedsPerMin)}/min) · {report.failed} failed
          </>
        ) : (
          <>
            +{report.added} added · {report.updated} updated · −{report.removed} removed · {report.failed} failed · {fmtDuration(report.duration_sec)} ({fmtRate(scansPerMin)} scans/min · {fmtRate(embedsPerMin)} emb/min)
          </>
        )}
      </span>
    </div>
  )
}
export { getReportRates }
