import type { BrowseFilters } from '../types'
import { CloseIcon } from './Icon'

interface Props {
  filters: BrowseFilters
  onRemove: (key: string) => void
  onClearAll: () => void
}

function labelForKey(key: string, value: unknown): string {
  switch (key) {
    case 'filename': return `Filename: "${value}"`
    case 'q': return `Filename: "${value}"`
    case 'ext': return `Ext: ${value}`
    case 'folder': return `Folder: "${value}"`
    case 'xmp': return `XMP: "${value}"`
    case 'xmp_query': return `XMP: "${value}"`
    case 'has_xmp': return 'Has XMP'
    case 'size_min': return `Min size: ${Math.round(Number(value) / (1024 * 1024))} MB`
    case 'size_max': return `Max size: ${Math.round(Number(value) / (1024 * 1024))} MB`
    case 'width_min': return `Min width: ${value}`
    case 'width_max': return `Max width: ${value}`
    case 'height_min': return `Min height: ${value}`
    case 'height_max': return `Max height: ${value}`
    case 'indexed_from': return `Indexed from: ${String(value).slice(0, 10)}`
    case 'indexed_to': return `Indexed to: ${String(value).slice(0, 10)}`
    case 'mtime_from': return `Modified from: ${new Date(Number(value) * 1000).toLocaleDateString()}`
    case 'mtime_to': return `Modified to: ${new Date(Number(value) * 1000).toLocaleDateString()}`
    default: return `${key}: ${String(value)}`
  }
}

export default function ActiveFilterChips({ filters, onRemove, onClearAll }: Props) {
  const entries = Object.entries(filters).filter(([, v]) => v !== undefined && v !== null && v !== '')
  if (entries.length === 0) return null

  return (
    <div className="filter-chips">
      {entries.map(([key]) => {
        const label = labelForKey(key, filters[key as keyof BrowseFilters])
        return (
          <span key={key} className="filter-chip">
            <span className="filter-chip-label">{label}</span>
            <button
              type="button"
              className="filter-chip-remove"
              onClick={() => onRemove(key)}
              aria-label={`Remove filter: ${label}`}
              title="Remove"
            >
              <CloseIcon width="12" height="12" />
            </button>
          </span>
        )
      })}
      {entries.length > 1 && (
        <button type="button" className="btn btn-sm" onClick={onClearAll}>
          Clear all
        </button>
      )}
    </div>
  )
}
