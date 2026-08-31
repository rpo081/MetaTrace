import type { BrowseImage, SearchResult } from '../types'
import { basename, formatBytes, formatDate, formatExt, xmpValue } from '../utils/format'
import AuthenticatedImage from './AuthenticatedImage'

interface Props {
  results: (SearchResult | BrowseImage)[]
  selectedId: number | null
  onSelect: (r: SearchResult | BrowseImage) => void
}

function formatDims(r: SearchResult | BrowseImage): string | null {
  return r.width != null && r.height != null ? `${r.width}×${r.height}` : null
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
              <AuthenticatedImage
                className="list-thumb"
                src={r.thumb_url}
                loading="lazy"
                alt=""
                onError={(e) => { (e.currentTarget as HTMLImageElement).style.display = 'none' }}
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
