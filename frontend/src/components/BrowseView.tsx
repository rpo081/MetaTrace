import { useCallback, useEffect, useRef, useState } from 'react'
import { browseImages } from '../api'
import type {
  BrowseFilters,
  BrowseImage,
  BrowseResponse,
  BrowseSort,
  BrowseOrder,
  ViewMode,
} from '../types'
import FilterSidebar from './FilterSidebar'
import ActiveFilterChips from './ActiveFilterChips'
import Pagination from './Pagination'
import ResultGrid from './ResultGrid'
import ResultList from './ResultList'
import ViewToggle from './ViewToggle'
import DetailPanel from './DetailPanel'
import { SortAscIcon, SortDescIcon } from './Icon'
import { BROWSE_VIEW_MODE_KEY, loadViewMode, saveViewMode } from '../lib/storage'

const SORT_OPTIONS: Array<{ value: BrowseSort; label: string }> = [
  { value: 'indexed_at', label: 'Date indexed' },
  { value: 'mtime', label: 'Date modified' },
  { value: 'size', label: 'File size' },
  { value: 'rel_path', label: 'Filename' },
  { value: 'width', label: 'Width' },
  { value: 'height', label: 'Height' },
  { value: 'id', label: 'ID' },
]

export default function BrowseView() {
  const [filters, setFilters] = useState<BrowseFilters>({})
  const [viewMode, setViewMode] = useState<ViewMode>(() => loadViewMode(BROWSE_VIEW_MODE_KEY))
  const [sort, setSort] = useState<BrowseSort>('mtime')
  const [order, setOrder] = useState<BrowseOrder>('desc')
  const [offset, setOffset] = useState(0)
  const limit = 60
  const [data, setData] = useState<BrowseResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [selectedId, setSelectedId] = useState<number | null>(null)

  const abortRef = useRef<AbortController | null>(null)
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const prevQRef = useRef<BrowseFilters['q']>(undefined)

  // Persist view mode via central storage abstraction
  useEffect(() => {
    saveViewMode(BROWSE_VIEW_MODE_KEY, viewMode)
  }, [viewMode])

  const fetchData = useCallback(
    (f: BrowseFilters, s: BrowseSort, o: BrowseOrder, off: number) => {
      abortRef.current?.abort()
      const controller = new AbortController()
      abortRef.current = controller
      setLoading(true)
      setError(null)
      browseImages({ offset: off, limit, sort: s, order: o, filters: f }, controller.signal)
        .then((res) => {
          if (!controller.signal.aborted) setData(res)
        })
        .catch((e) => {
          if (controller.signal.aborted) return
          if (e instanceof DOMException && e.name === 'AbortError') return
          setError(e instanceof Error ? e.message : String(e))
        })
        .finally(() => {
          if (abortRef.current === controller) setLoading(false)
        })
    },
    [],
  )

  // Fetch on filter/sort/order/offset change, but debounce text-query requests
  useEffect(() => {
    const qChanged = filters.q !== prevQRef.current
    prevQRef.current = filters.q

    if (debounceRef.current) {
      clearTimeout(debounceRef.current)
      debounceRef.current = null
    }

    if (qChanged) {
      debounceRef.current = setTimeout(() => {
        fetchData(filters, sort, order, offset)
        debounceRef.current = null
      }, 300)

      return () => {
        if (debounceRef.current) {
          clearTimeout(debounceRef.current)
          debounceRef.current = null
        }
      }
    }

    fetchData(filters, sort, order, offset)
  }, [filters, sort, order, offset, fetchData])

  // Cleanup abort/timer on unmount
  useEffect(
    () => () => {
      abortRef.current?.abort()
      if (debounceRef.current) {
        clearTimeout(debounceRef.current)
      }
    },
    [],
  )

  // Close detail panel on Escape
  useEffect(() => {
    if (!selectedId) return
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setSelectedId(null)
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [selectedId])

  const onFiltersChange = useCallback(
    (next: BrowseFilters) => {
      setFilters(next)
      setOffset(0)
      setSelectedId(null)
    },
    [],
  )

  const onRemoveFilter = useCallback(
    (key: string) => {
      const next = { ...filters }
      delete (next as Record<string, unknown>)[key]
      setFilters(next)
      setOffset(0)
      setSelectedId(null)
    },
    [filters],
  )

  const onClearFilters = useCallback(() => {
    setFilters({})
    setOffset(0)
    setSelectedId(null)
  }, [])

  const selectedImage = data?.items.find((i) => i.id === selectedId) ?? null
  const hasActiveFilters = Object.keys(filters).length > 0

  // Build results array compatible with ResultGrid/ResultList
  const results: BrowseImage[] = data?.items ?? []

  return (
    <main id="main-content" className="browse-layout">
      <aside className="browse-sidebar">
        <FilterSidebar filters={filters} onChange={onFiltersChange} />
      </aside>

      <section className="browse-content">
        {/* Toolbar */}
        <div className="browse-toolbar">
          <ViewToggle mode={viewMode} onChange={setViewMode} />
          <div className="browse-sort">
            <label htmlFor="browse-sort-select" className="muted browse-sort-label">
              Sort:
            </label>
            <div className="browse-sort-controls">
              <select
                id="browse-sort-select"
                className="text-input browse-sort-select"
                value={sort}
                onChange={(e) => { setSort(e.target.value as BrowseSort); setOffset(0) }}
              >
                {SORT_OPTIONS.map((opt) => (
                  <option key={opt.value} value={opt.value}>{opt.label}</option>
                ))}
              </select>
              <button
                type="button"
                className="btn browse-sort-order-btn"
                onClick={() => {
                  setOrder((o) => (o === 'desc' ? 'asc' : 'desc'))
                  setOffset(0)
                }}
                title={`Current order: ${order}. Click to toggle.`}
                aria-label={`Toggle sort direction, currently ${order === 'desc' ? 'descending' : 'ascending'}`}
              >
                {order === 'desc' ? <SortDescIcon width="16" height="16" /> : <SortAscIcon width="16" height="16" />}
              </button>
            </div>
          </div>
        </div>

        {/* Active filter chips */}
        <ActiveFilterChips filters={filters} onRemove={onRemoveFilter} onClearAll={onClearFilters} />

        {/* Error */}
        {error && <div className="error-box" role="alert">Failed to load images: {error}</div>}

        {/* Loading */}
        {loading && (
          <div className="busy-overlay" role="status" aria-live="polite">
            <span className="spinner" aria-hidden />
            Loading…
          </div>
        )}

        {/* Results */}
        {!loading && results.length === 0 && (
          <div className="placeholder">
            <p>{hasActiveFilters ? 'No images match the current filters.' : 'No images in the index yet.'}</p>
          </div>
        )}

        {results.length > 0 && (
          <>
            {selectedId && selectedImage ? (
              <div className="split split-browse">
                {viewMode === 'grid' ? (
                  <ResultGrid results={results} selectedId={selectedId} onSelect={(r) => setSelectedId(r.id)} />
                ) : (
                  <ResultList results={results} selectedId={selectedId} onSelect={(r) => setSelectedId(r.id)} />
                )}
                <DetailPanel result={selectedImage} onClose={() => setSelectedId(null)} />
              </div>
            ) : viewMode === 'grid' ? (
              <ResultGrid results={results} selectedId={selectedId} onSelect={(r) => setSelectedId(r.id)} />
            ) : (
              <ResultList results={results} selectedId={selectedId} onSelect={(r) => setSelectedId(r.id)} />
            )}
          </>
        )}

        {/* Pagination */}
        {data && (
          <Pagination
            offset={data.offset}
            limit={data.limit}
            total={data.total}
            hasMore={data.has_more}
            onPrev={() => { setOffset((o) => Math.max(0, o - limit)); setSelectedId(null) }}
            onNext={() => { setOffset((o) => o + limit); setSelectedId(null) }}
          />
        )}
      </section>
    </main>
  )
}
