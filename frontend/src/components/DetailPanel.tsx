import { useState } from 'react'
import { authenticatedUrl } from '../api'
import type { BrowseImage, SearchResult } from '../types'
import { CloseIcon, ExternalLinkIcon } from './Icon'

interface Props {
  result: SearchResult | BrowseImage
  onClose: () => void
}

const XMP_PRIORITY = ['Creator', 'Description'] as const

function fmtValue(v: unknown): string {
  if (Array.isArray(v)) return v.join('; ')
  if (typeof v === 'object' && v !== null) return JSON.stringify(v)
  return String(v)
}

function detailThumbnailUrl(thumbUrl: string): string {
  const url = new URL(thumbUrl, window.location.origin)
  url.searchParams.set('size', '512')
  // authenticatedUrl appends token; do it after setting size
  const raw = `${url.pathname}${url.search}`
  // inline to avoid circular import at top-level; dynamic import via helper
  try {
    const token = (() => {
      try {
        return localStorage.getItem('metatrace_access_token') || localStorage.getItem('metatrace_admin_token')
      } catch { return null }
    })()
    if (token) {
      url.searchParams.set('token', token)
      return `${url.pathname}${url.search}`
    }
  } catch { /* ignore */ }
  return raw
}

function matchesXmpAttribute(key: string, attribute: string): boolean {
  const keyLower = key.toLowerCase()
  const attrLower = attribute.toLowerCase()
  if (keyLower.endsWith(attrLower)) return true
  const tail = keyLower.split(':').pop() ?? keyLower
  return tail === attrLower
}

function sortedXmpEntries(xmp: Record<string, unknown>): Array<[string, unknown]> {
  const entries = Object.entries(xmp)
  if (entries.length === 0) return []

  const prioritized: Array<[string, unknown]> = []
  const rest = [...entries]

  for (const attr of XMP_PRIORITY) {
    const idx = rest.findIndex(([key]) => matchesXmpAttribute(key, attr))
    if (idx !== -1) {
      const [key, value] = rest.splice(idx, 1)[0]
      prioritized.push([attr, value])
    }
  }

  return [...prioritized, ...rest]
}

function toWindowsPath(path: string): string {
  return path.replaceAll('/', '\\')
}

function parentFolder(path: string): string {
  const windowsPath = toWindowsPath(path)
  const idx = windowsPath.lastIndexOf('\\')
  if (idx <= 0) return windowsPath
  return windowsPath.slice(0, idx)
}

function toFileUri(path: string): string | null {
  const windowsPath = toWindowsPath(path)
  // UNC path: \\server\share\folder -> file://server/share/folder
  if (windowsPath.startsWith('\\\\')) {
    const unc = windowsPath.slice(2).replaceAll('\\', '/')
    return `file://${encodeURI(unc)}`
  }
  // Drive path: C:\folder -> file:///C:/folder
  if (/^[a-zA-Z]:\\/.test(windowsPath)) {
    return `file:///${encodeURI(windowsPath.replaceAll('\\', '/'))}`
  }
  return null
}

async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text)
    return true
  } catch {
    const area = document.createElement('textarea')
    area.value = text
    area.setAttribute('readonly', '')
    area.style.position = 'fixed'
    area.style.left = '-9999px'
    document.body.appendChild(area)
    area.select()
    let ok = false
    try {
      ok = document.execCommand('copy')
    } finally {
      document.body.removeChild(area)
    }
    return ok
  }
}

export default function DetailPanel({ result, onClose }: Props) {
  const xmpEntries = sortedXmpEntries(result.xmp ?? {})
  const [copiedPath, setCopiedPath] = useState(false)
  const [copiedFolder, setCopiedFolder] = useState(false)
  const hasScore = 'score' in result && result.score != null
  const score = hasScore ? (result as SearchResult).score : 0
  const exact = 'exact' in result ? (result as SearchResult).exact : false

  const onCopyOriginalPath = async () => {
    const ok = await copyText(toWindowsPath(result.original_path))
    setCopiedPath(ok)
    if (ok) {
      window.setTimeout(() => setCopiedPath(false), 1500)
    }
  }

  const onOpenFolder = async () => {
    const folder = parentFolder(result.original_path)
    const uri = toFileUri(folder)
    if (uri) {
      const opened = window.open(uri, '_blank', 'noopener,noreferrer')
      if (opened) return
    }
    // Browser blocked file:// — copy path and show toast
    const ok = await copyText(folder)
    setCopiedFolder(ok)
    if (ok) {
      window.setTimeout(() => setCopiedFolder(false), 2500)
    }
  }

  return (
    <aside className="detail-panel">
      <div className="detail-header">
        <h2>Details</h2>
        <button className="btn btn-ghost btn-icon" onClick={onClose} aria-label="Close">
          <CloseIcon width="18" height="18" />
        </button>
      </div>

      <img
        className="detail-img"
        src={detailThumbnailUrl(result.thumb_url)}
        alt={result.rel_path}
        onError={(e) => { e.currentTarget.style.display = 'none' }}
      />

      <div className="detail-section">
        <div className="kv">
          <span className="k">File</span>
          <span className="v mono selectable">{result.rel_path}</span>
        </div>
        <div className="kv">
          <span className="k">Original path</span>
          <span className="v">
            <button
              type="button"
              className="path-copy mono selectable"
              onClick={onCopyOriginalPath}
              title="Click to copy Windows Explorer path"
            >
              {toWindowsPath(result.original_path)}
            </button>
            {copiedPath && <span className="muted"> copied</span>}
            <button
              type="button"
              className="btn btn-ghost detail-open-folder"
              onClick={onOpenFolder}
              title="Open containing folder in Explorer (or copy folder path if blocked)"
            >
              Open folder
            </button>
            {copiedFolder && (
              <span className="info-box detail-inline-notice" role="status">
                Path copied to clipboard
              </span>
            )}
          </span>
        </div>
        {hasScore && (
          <div className="kv">
            <span className="k">Score</span>
            <span className="v">
              {(score * 100).toFixed(1)}%{exact && ' (exact byte match)'}
            </span>
          </div>
        )}
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
            <a href={authenticatedUrl(result.file_url)} target="_blank" rel="noreferrer">
              original file <ExternalLinkIcon width="12" height="12" className="icon-inline" />
            </a>
          </span>
        </div>
      </div>

      <div className="detail-section">
        <h3>XMP tags {xmpEntries.length === 0 && <span className="muted">(none)</span>}</h3>
        {xmpEntries.length > 0 && (
          <table className="xmp-table">
            <thead>
              <tr>
                <th scope="col">Key</th>
                <th scope="col">Value</th>
              </tr>
            </thead>
            <tbody>
              {xmpEntries.map(([key, value]) => (
                <tr key={key}>
                  <th scope="row" className="mono">{key}</th>
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
