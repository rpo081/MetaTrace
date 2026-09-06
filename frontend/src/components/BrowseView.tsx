import { useCallback, useEffect, useRef, useState } from 'react'
import { browseImages, prewarmThumbnails } from '../api'
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
import ImageViewerModal from './ImageViewerModal'
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
  const prevTextFiltersRef = useRef<{
    filename?: string
    q?: string
    folder?: string
    xmp?: string
    xmp_query?: string
  }>({})

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
    const textChanged =
      filters.filename !== prevTextFiltersRef.current.filename ||
      filters.q !== prevTextFiltersRef.current.q ||
      filters.folder !== prevTextFiltersRef.current.folder ||
      filters.xmp !== prevTextFiltersRef.current.xmp ||
      filters.xmp_query !== prevTextFiltersRef.current.xmp_query

    prevTextFiltersRef.current = {
      filename: filters.filename,
      q: filters.q,
      folder: filters.folder,
      xmp: filters.xmp,
      xmp_query: filters.xmp_query,
    }

    if (debounceRef.current) {
      clearTimeout(debounceRef.current)
      debounceRef.current = null
    }

    if (textChanged) {
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

  // Close detail panel on Escape (not while the image viewer is open —
  // the viewer handles Escape itself).
  const [viewer, setViewer] = useState<{ images: BrowseImage[]; index: number } | null>(null)
  useEffect(() => {
    if (!selectedId || viewer) return
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setSelectedId(null)
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [selectedId, viewer])

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
  const resultsRef = useRef<BrowseImage[]>([])
  resultsRef.current = results
  const selectedIdRef = useRef<number | null>(null)
  selectedIdRef.current = selectedId

  const openViewer = useCallback((i: number) => {
    setViewer({ images: resultsRef.current, index: i })
  }, [])
  const closeViewer = useCallback(() => {
    setViewer(null)
  }, [])
  const openDetailViewer = useCallback(() => {
    const item = resultsRef.current.find((r) => r.id === selectedIdRef.current)
    if (item) setViewer({ images: [item], index: 0 })
  }, [])
  const handleViewerNavigate = useCallback((next: number) => {
    setViewer((v) => {
      if (!v || next < 0 || next >= v.images.length) return v
      // Mirror the focused image in the background detail panel.
      const item = v.images[next]
      if (item) setSelectedId(item.id)
      return { images: v.images, index: next }
    })
  }, [])

  useEffect(() => {
    void prewarmThumbnails(loading ? [] : results.map((result) => result.id), 512).catch(() => {})
  }, [loading, results])

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
                  <ResultGrid results={results} selectedId={selectedId} onSelect={(r) => setSelectedId(r.id)} onOpenViewer={openViewer} />
                ) : (
                  <ResultList results={results} selectedId={selectedId} onSelect={(r) => setSelectedId(r.id)} onOpenViewer={openViewer} />
                )}
                <DetailPanel result={selectedImage} onClose={() => setSelectedId(null)} onOpenViewer={openDetailViewer} />
              </div>
            ) : viewMode === 'grid' ? (
              <ResultGrid results={results} selectedId={selectedId} onSelect={(r) => setSelectedId(r.id)} onOpenViewer={openViewer} />
            ) : (
              <ResultList results={results} selectedId={selectedId} onSelect={(r) => setSelectedId(r.id)} onOpenViewer={openViewer} />
            )}
            {viewer && (
              <ImageViewerModal
                open
                images={viewer.images}
                index={viewer.index}
                onClose={closeViewer}
                onNavigate={handleViewerNavigate}
              />
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
