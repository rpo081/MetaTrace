import type { SearchResult } from '../types'

interface Props {
  result: SearchResult
  onClose: () => void
}

function fmtValue(v: unknown): string {
  if (Array.isArray(v)) return v.join('; ')
  if (typeof v === 'object' && v !== null) return JSON.stringify(v)
  return String(v)
}

export default function DetailPanel({ result, onClose }: Props) {
  const xmpEntries = Object.entries(result.xmp ?? {})
  return (
    <aside className="detail-panel">
      <div className="detail-header">
        <h2>Details</h2>
        <button className="btn btn-ghost" onClick={onClose} aria-label="Close">
          ✕
        </button>
      </div>

      <img
        className="detail-img"
        src={`${result.thumb_url}?size=1024`}
        alt={result.rel_path}
      />

      <div className="detail-section">
        <div className="kv">
          <span className="k">File</span>
          <span className="v mono">{result.rel_path}</span>
        </div>
        <div className="kv">
          <span className="k">Original path</span>
          <span className="v mono selectable">{result.original_path}</span>
        </div>
        <div className="kv">
          <span className="k">Score</span>
          <span className="v">
            {(result.score * 100).toFixed(1)}%{result.exact && ' (exact byte match)'}
          </span>
        </div>
        {result.width != null && (
          <div className="kv">
            <span className="k">Dimensions</span>
            <span className="v">
              {result.width} × {result.height}
            </span>
          </div>
        )}
        <div className="kv">
          <span className="k">Open</span>
          <span className="v">
            <a href={result.file_url} target="_blank" rel="noreferrer">
              original file ↗
            </a>
          </span>
        </div>
      </div>

      <div className="detail-section">
        <h3>XMP tags {xmpEntries.length === 0 && <span className="muted">(none)</span>}</h3>
        {xmpEntries.length > 0 && (
          <table className="xmp-table">
            <tbody>
              {xmpEntries.map(([key, value]) => (
                <tr key={key}>
                  <td className="mono">{key}</td>
                  <td className="selectable">{fmtValue(value)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </aside>
  )
}
