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
  return dot >= 0 ? p.slice(dot).toLowerCase() : ''
}

function formatDate(val: string | number | undefined): string | null {
  if (val == null) return null
  const d = new Date(typeof val === 'number' ? val * 1000 : val)
  if (isNaN(d.getTime())) return null
  return d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' })
}

function metaLine(r: SearchResult | BrowseImage): string {
  const parts: string[] = []
  const dims = formatDims(r)
  if (dims) parts.push(dims)
  if ('size' in r && r.size != null) parts.push(formatBytes(r.size))
  const ext = formatExt(r.rel_path)
  if (ext) parts.push(ext)
  const date = 'indexed_at' in r ? r.indexed_at : undefined
  const fmt = formatDate(date)
  if (fmt) parts.push(fmt)
  if ('score' in r && r.score != null) parts.push(`${Math.round(r.score * 100)}%`)
  return parts.join(' · ')
}

export default function ResultList({ results, selectedId, onSelect }: Props) {
  return (
    <div className="result-list" role="list">
      {results.map((r) => {
        const isSelected = selectedId === r.id
        return (
          <div key={r.id} className="result-list-item" role="listitem">
            <button
              type="button"
              className={`list-row ${isSelected ? 'list-row-selected' : ''}`}
              aria-pressed={isSelected}
              aria-label={basename(r.rel_path)}
              onClick={() => onSelect(r)}
            >
              <img
                className="list-thumb"
                src={r.thumb_url}
                loading="lazy"
                alt=""
                onError={(e) => { e.currentTarget.style.display = 'none' }}
              />
              <span className="list-info">
                <span className="list-name" title={r.rel_path}>{basename(r.rel_path)}</span>
                <span className="list-meta muted">{metaLine(r)}</span>
              </span>
            </button>
          </div>
        )
      })}
    </div>
  )
}
