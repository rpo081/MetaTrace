import { useCallback } from 'react'
import type { BrowseFilters } from '../types'

interface Props {
  filters: BrowseFilters
  onChange: (filters: BrowseFilters) => void
}

const EXTENSIONS = ['.psd', '.jpg', '.png', '.tif']

export default function FilterSidebar({ filters, onChange }: Props) {
  const set = useCallback(
    (key: keyof BrowseFilters, value: BrowseFilters[keyof BrowseFilters]) => {
      onChange({ ...filters, [key]: value ?? undefined })
    },
    [filters, onChange],
  )

  const clearSection = useCallback(
    (keys: (keyof BrowseFilters)[]) => {
      const next = { ...filters }
      for (const k of keys) delete (next as Record<string, unknown>)[k]
      onChange(next)
    },
    [filters, onChange],
  )

  const hasActiveFilters = Object.keys(filters).length > 0

  return (
    <div className="filter-sidebar">
      {hasActiveFilters && (
        <button type="button" className="btn btn-sm clear-all-filters-btn" onClick={() => onChange({})}>
          Clear all filters
        </button>
      )}

      {/* Search */}
      <details open>
        <summary>Search</summary>
        <div className="filter-section">
          <input
            id="filter-search"
            type="text"
            className="text-input"
            placeholder="Filename or XMP…"
            value={filters.q ?? ''}
            onChange={(e) => set('q', e.target.value || undefined)}
          />
          <label className="filter-check" htmlFor="filter-has-xmp">
            <input
              id="filter-has-xmp"
              type="checkbox"
              checked={filters.has_xmp ?? false}
              onChange={(e) => set('has_xmp', e.target.checked || undefined)}
            />
            Has XMP data
          </label>
          <button type="button" className="btn btn-sm" onClick={() => clearSection(['q', 'has_xmp'])}>Clear</button>
        </div>
      </details>

      {/* Folder */}
      <details>
        <summary>Folder</summary>
        <div className="filter-section">
          <input
            id="filter-folder"
            type="text"
            className="text-input"
            placeholder="Folder prefix…"
            value={filters.folder ?? ''}
            onChange={(e) => set('folder', e.target.value || undefined)}
          />
          <button type="button" className="btn btn-sm" onClick={() => clearSection(['folder'])}>Clear</button>
        </div>
      </details>

      {/* Extension */}
      <details>
        <summary>Extension</summary>
        <div className="filter-section">
          <div className="filter-chips-inline">
            {EXTENSIONS.map((ext) => (
              <button
                key={ext}
                type="button"
                className={`toggle-chip ${filters.ext === ext ? 'toggle-chip-active' : ''}`}
                onClick={() => set('ext', filters.ext === ext ? undefined : ext)}
                aria-pressed={filters.ext === ext}
              >
                {ext}
              </button>
            ))}
          </div>
          <button type="button" className="btn btn-sm" onClick={() => clearSection(['ext'])}>Clear</button>
        </div>
      </details>

      {/* File Size */}
      <details>
        <summary>File Size</summary>
        <div className="filter-section">
          <div className="filter-field-row">
            <label className="filter-field" htmlFor="filter-size-min">
              <span className="muted filter-label-small">Min (MB)</span>
              <input
                id="filter-size-min"
                type="number"
                className="text-input"
                min={0}
                placeholder="0"
                value={filters.size_min != null ? String(Math.round(filters.size_min / (1024 * 1024))) : ''}
                onChange={(e) => {
                  const v = e.target.value ? parseInt(e.target.value, 10) : undefined
                  set('size_min', v != null && !isNaN(v) ? v * 1024 * 1024 : undefined)
                }}
              />
            </label>
            <label className="filter-field" htmlFor="filter-size-max">
              <span className="muted filter-label-small">Max (MB)</span>
              <input
                id="filter-size-max"
                type="number"
                className="text-input"
                min={0}
                placeholder="∞"
                value={filters.size_max != null ? String(Math.round(filters.size_max / (1024 * 1024))) : ''}
                onChange={(e) => {
                  const v = e.target.value ? parseInt(e.target.value, 10) : undefined
                  set('size_max', v != null && !isNaN(v) ? v * 1024 * 1024 : undefined)
                }}
              />
            </label>
          </div>
          <button type="button" className="btn btn-sm" onClick={() => clearSection(['size_min', 'size_max'])}>Clear</button>
        </div>
      </details>

      {/* Dimensions */}
      <details>
        <summary>Dimensions</summary>
        <div className="filter-section">
          <div className="filter-field-row">
            <label className="filter-field" htmlFor="filter-width-min">
              <span className="muted filter-label-small">Min width</span>
              <input
                id="filter-width-min"
                type="number"
                className="text-input"
                min={0}
                placeholder="0"
                value={filters.width_min ?? ''}
                onChange={(e) => set('width_min', e.target.value ? parseInt(e.target.value, 10) : undefined)}
              />
            </label>
            <label className="filter-field" htmlFor="filter-width-max">
              <span className="muted filter-label-small">Max width</span>
              <input
                id="filter-width-max"
                type="number"
                className="text-input"
                min={0}
                placeholder="∞"
                value={filters.width_max ?? ''}
                onChange={(e) => set('width_max', e.target.value ? parseInt(e.target.value, 10) : undefined)}
              />
            </label>
          </div>
          <div className="filter-field-row">
            <label className="filter-field" htmlFor="filter-height-min">
              <span className="muted filter-label-small">Min height</span>
              <input
                id="filter-height-min"
                type="number"
                className="text-input"
                min={0}
                placeholder="0"
                value={filters.height_min ?? ''}
                onChange={(e) => set('height_min', e.target.value ? parseInt(e.target.value, 10) : undefined)}
              />
            </label>
            <label className="filter-field" htmlFor="filter-height-max">
              <span className="muted filter-label-small">Max height</span>
              <input
                id="filter-height-max"
                type="number"
                className="text-input"
                min={0}
                placeholder="∞"
                value={filters.height_max ?? ''}
                onChange={(e) => set('height_max', e.target.value ? parseInt(e.target.value, 10) : undefined)}
              />
            </label>
          </div>
          <button type="button" className="btn btn-sm" onClick={() => clearSection(['width_min', 'width_max', 'height_min', 'height_max'])}>Clear</button>
        </div>
      </details>

      {/* Date Indexed */}
      <details>
        <summary>Date Indexed</summary>
        <div className="filter-section">
          <label className="filter-field" htmlFor="filter-indexed-from">
            <span className="muted filter-label-small">From</span>
            <input
              id="filter-indexed-from"
              type="datetime-local"
              className="text-input"
              value={filters.indexed_from ?? ''}
              onChange={(e) => set('indexed_from', e.target.value || undefined)}
            />
          </label>
          <label className="filter-field" htmlFor="filter-indexed-to">
            <span className="muted filter-label-small">To</span>
            <input
              id="filter-indexed-to"
              type="datetime-local"
              className="text-input"
              value={filters.indexed_to ?? ''}
              onChange={(e) => set('indexed_to', e.target.value || undefined)}
            />
          </label>
          <button type="button" className="btn btn-sm" onClick={() => clearSection(['indexed_from', 'indexed_to'])}>Clear</button>
        </div>
      </details>

      {/* Date Modified */}
      <details>
        <summary>Date Modified</summary>
        <div className="filter-section">
          <label className="filter-field" htmlFor="filter-mtime-from">
            <span className="muted filter-label-small">From</span>
            <input
              id="filter-mtime-from"
              type="datetime-local"
              className="text-input"
              value={filters.mtime_from != null ? new Date(filters.mtime_from * 1000).toISOString().slice(0, 16) : ''}
              onChange={(e) => set('mtime_from', e.target.value ? new Date(e.target.value).getTime() / 1000 : undefined)}
            />
          </label>
          <label className="filter-field" htmlFor="filter-mtime-to">
            <span className="muted filter-label-small">To</span>
            <input
              id="filter-mtime-to"
              type="datetime-local"
              className="text-input"
              value={filters.mtime_to != null ? new Date(filters.mtime_to * 1000).toISOString().slice(0, 16) : ''}
              onChange={(e) => set('mtime_to', e.target.value ? new Date(e.target.value).getTime() / 1000 : undefined)}
            />
          </label>
          <button type="button" className="btn btn-sm" onClick={() => clearSection(['mtime_from', 'mtime_to'])}>Clear</button>
        </div>
      </details>
    </div>
  )
}
