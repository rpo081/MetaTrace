import { authenticatedUrl } from '../api'
import type { BrowseImage, SearchResult } from '../types'

interface Props {
  results: (SearchResult | BrowseImage)[]
  selectedId: number | null
  onSelect: (r: SearchResult | BrowseImage) => void
}

function basename(p: string): string {
  return p.split(/[\\/]/).pop() ?? p
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(1)} GB`
}

function formatDims(r: SearchResult | BrowseImage): string | null {
  if (r.width != null && r.height != null) return `${r.width}×${r.height}`
  return null
}

function formatExt(p: string): string {
  const dot = p.lastIndexOf('.')
  return dot >= 0 ? p.slice(dot + 1).toUpperCase() : ''
}

function formatDate(val: string | number | undefined): string | null {
  if (val == null) return null
  const d = new Date(typeof val === 'number' ? val * 1000 : val)
  if (isNaN(d.getTime())) return null
  return d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' })
}

function fmtValue(v: unknown): string {
  if (Array.isArray(v)) return v.join('; ')
  if (typeof v === 'object' && v !== null) return JSON.stringify(v)
  return String(v)
}

function xmpValue(xmp: Record<string, unknown> | undefined, attribute: string): string | null {
  if (!xmp) return null
  const attrLower = attribute.toLowerCase()
  for (const [key, value] of Object.entries(xmp)) {
    const tail = key.toLowerCase().split(':').pop() ?? key.toLowerCase()
    if (tail === attrLower) return fmtValue(value)
  }
  return null
}

export default function ResultList({ results, selectedId, onSelect }: Props) {
  return (
    <div className="result-list" role="list">
      {results.map((r) => {
        const isSelected = selectedId === r.id
        const dims = formatDims(r)
        const ext = formatExt(r.rel_path)
        const fileSize = 'size' in r && r.size != null ? formatBytes(r.size) : null

        const xmpCreated = xmpValue(r.xmp, 'CreateDate') ?? xmpValue(r.xmp, 'Created')
        const mtime = 'mtime' in r ? (r as BrowseImage).mtime : undefined
        const dateStr = xmpCreated
          ? formatDate(xmpCreated)
          : formatDate(mtime)

        const creator = xmpValue(r.xmp, 'Creator')
        const description = xmpValue(r.xmp, 'Description')
        const subject = xmpValue(r.xmp, 'Subject')
        const transmissionRef = xmpValue(r.xmp, 'TransmissionReference')
        const metadataDate = xmpValue(r.xmp, 'MetadataDate')

        const tags = [
          { label: 'Creator', value: creator },
          { label: 'Description', value: description },
          { label: 'Subject', value: subject },
          { label: 'Transmission', value: transmissionRef },
          { label: 'Metadata Date', value: metadataDate },
        ].filter((t) => t.value)

        const ariaParts = [basename(r.rel_path)]
        if (dims) ariaParts.push(dims)
        if (fileSize) ariaParts.push(fileSize)
        if (dateStr) ariaParts.push(dateStr)
        if (creator) ariaParts.push(`Creator: ${creator}`)
        if (description) ariaParts.push(`Description: ${description}`)
        if (subject) ariaParts.push(`Subject: ${subject}`)
        if (transmissionRef) ariaParts.push(`Transmission: ${transmissionRef}`)

        return (
          <div key={r.id} className="result-list-item" role="listitem">
            <button
              type="button"
              className={`list-row ${isSelected ? 'list-row-selected' : ''}`}
              aria-pressed={isSelected}
              aria-label={ariaParts.join(', ')}
              onClick={() => onSelect(r)}
            >
              <img
                className="list-thumb"
                src={authenticatedUrl(r.thumb_url)}
                loading="lazy"
                alt=""
                onError={(e) => { e.currentTarget.style.display = 'none' }}
              />
              <span className="list-info">
                <span className="list-name" title={r.rel_path}>
                  {basename(r.rel_path)}
                </span>
                <span className="list-path mono" title={r.rel_path}>
                  {r.rel_path}
                </span>
                <span className="list-details muted">
                  {dims && <span>{dims}</span>}
                  {dims && ext && <span className="list-detail-sep" aria-hidden>·</span>}
                  {ext && <span>{ext}</span>}
                  {(dims || ext) && fileSize && <span className="list-detail-sep" aria-hidden>·</span>}
                  {fileSize && <span>{fileSize}</span>}
                  {(dims || ext || fileSize) && dateStr && <span className="list-detail-sep" aria-hidden>·</span>}
                  {dateStr && <span>{dateStr}</span>}
                </span>
                {tags.length > 0 && (
                  <span className="list-tags">
                    {tags.map((t) => (
                      <span key={t.label} className="list-tag" title={`${t.label}: ${t.value}`}>
                        <span className="list-tag-label">{t.label}:</span>{' '}
                        <span className="list-tag-value">{t.value}</span>
                      </span>
                    ))}
                  </span>
                )}
                {'score' in r && r.score != null && (
                  <span className="list-score muted">
                    {Math.round(r.score * 100)}% match
                  </span>
                )}
              </span>
            </button>
          </div>
        )
      })}
    </div>
  )
}
