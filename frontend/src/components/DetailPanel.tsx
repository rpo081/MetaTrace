import { useState } from 'react'
import type { SearchResult } from '../types'

interface Props {
  result: SearchResult
  onClose: () => void
}

const XMP_WHITELIST = [
  'TransmissionReference',
  'MetadataDate',
  'Creator',
  'Description',
  'Subject',
] as const

function fmtValue(v: unknown): string {
  if (Array.isArray(v)) return v.join('; ')
  if (typeof v === 'object' && v !== null) return JSON.stringify(v)
  return String(v)
}

function detailThumbnailUrl(thumbUrl: string): string {
  const url = new URL(thumbUrl, window.location.origin)
  url.searchParams.set('size', '512')
  return `${url.pathname}${url.search}`
}

function matchesXmpAttribute(key: string, attribute: string): boolean {
  const keyLower = key.toLowerCase()
  const attrLower = attribute.toLowerCase()
  if (keyLower.endsWith(attrLower)) return true
  const tail = keyLower.split(':').pop() ?? keyLower
  return tail === attrLower
}

function selectedXmpEntries(xmp: Record<string, unknown>): Array<[string, unknown]> {
  const entries = Object.entries(xmp)
  return XMP_WHITELIST.flatMap((attribute) => {
    const found = entries.find(([key]) => matchesXmpAttribute(key, attribute))
    return found ? [[attribute, found[1]] as [string, unknown]] : []
  })
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
  const xmpEntries = selectedXmpEntries(result.xmp ?? {})
  const [copiedPath, setCopiedPath] = useState(false)
  const [copiedFolder, setCopiedFolder] = useState(false)

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
    // Browser may block file:// navigation from http(s); fallback is copy.
    const ok = await copyText(folder)
    setCopiedFolder(ok)
    if (ok) {
      window.setTimeout(() => setCopiedFolder(false), 1500)
    }
  }

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
        src={detailThumbnailUrl(result.thumb_url)}
        alt={result.rel_path}
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
              className="btn btn-ghost"
              onClick={onOpenFolder}
              title="Open containing folder in Explorer (or copy folder path if blocked)"
            >
              Open folder
            </button>
            {copiedFolder && <span className="muted"> folder copied</span>}
          </span>
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
