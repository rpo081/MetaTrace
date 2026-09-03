/** SearchView — sidebar (Dropzone + text query + controls) + content (busy
 *  overlay, results with sort, DetailPanel). All search state lives in
 *  useSearch; this component is responsible only for layout and the
 *  search-specific sort state. The App-level transient `notice` slot is
 *  rendered here because that's where the original App put it.
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import type { SearchResult, ViewMode, Stats } from '../../types'
import { prewarmThumbnails } from '../../api'
import Dropzone from '../../components/Dropzone'
import DetailPanel from '../../components/DetailPanel'
import ResultGrid from '../../components/ResultGrid'
import ResultList from '../../components/ResultList'
import ViewToggle from '../../components/ViewToggle'
import { CloseIcon, SortAscIcon, SortDescIcon } from '../../components/Icon'
import { VIEW_MODE_KEY, loadViewMode, saveViewMode } from '../../lib/storage'
import { useSearch } from './useSearch'

interface Props {
  stats: Stats | null
  refreshStats: () => void
  /** App-level transient notice (e.g. "Rescan started.", "Scan resumed."). */
  notice: string | null
  /** App-level transient error (e.g. rescan failure). */
  error: string | null
}

const SEARCH_SORT_OPTIONS = [
  { value: 'score', label: 'Relevance' },
  { value: 'rel_path', label: 'Filename' },
  { value: 'width', label: 'Width' },
  { value: 'height', label: 'Height' },
  { value: 'id', label: 'ID' },
]

export default function SearchView({
  stats,
  refreshStats,
  notice,
  error,
}: Props) {
  const search = useSearch({
    maxUploadMb: stats?.max_upload_mb,
    onSettled: refreshStats,
  })

  const [viewMode, setViewMode] = useState<ViewMode>(() => loadViewMode(VIEW_MODE_KEY))
  useEffect(() => saveViewMode(VIEW_MODE_KEY, viewMode), [viewMode])

  const [searchSort, setSearchSort] = useState('score')
  const [searchOrder, setSearchOrder] = useState<'asc' | 'desc'>('desc')

  const sortedResults = useMemo(() => {
    if (!search.response?.results) return []
    const sorted = [...search.response.results]
    sorted.sort((a, b) => {
      const aVal = a[searchSort as keyof SearchResult]
      const bVal = b[searchSort as keyof SearchResult]
      if (aVal == null && bVal == null) return 0
      if (aVal == null) return 1
      if (bVal == null) return -1
      if (typeof aVal === 'string' && typeof bVal === 'string') {
        return searchOrder === 'asc' ? aVal.localeCompare(bVal) : bVal.localeCompare(aVal)
      }
      const cmp = Number(aVal) - Number(bVal)
      return searchOrder === 'asc' ? cmp : -cmp
    })
    return sorted
  }, [search.response, searchSort, searchOrder])

  const prewarmIds = useMemo(
    () => sortedResults.map((result) => result.id),
    [sortedResults],
  )

  useEffect(() => {
    void prewarmThumbnails(search.loading ? [] : prewarmIds, 512).catch(() => {})
  }, [search.loading, prewarmIds])

  // Close detail panel on Escape
  useEffect(() => {
    if (!search.selected) return
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') search.setSelected(null)
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [search.selected, search.setSelected])

  const handleSelect = useCallback(
    (r: SearchResult | { id: number }) => {
      // ResultGrid/List pass either SearchResult or BrowseImage; here we only
      // accept SearchResult. The caller is search-only.
      if ('score' in r) search.setSelected(r as SearchResult)
    },
    [search.setSelected],
  )

  return (
    <main id="main-content" className="layout">
      <section className="sidebar">
        <Dropzone
          previewUrl={search.previewUrl}
          onFile={search.onFile}
          onClear={search.onClearFile}
          disabled={search.loading}
        />

        <div className="text-search-box">
          <label htmlFor="text-query-input" className="text-search-label">
            Text / Project / XMP Search
          </label>
          <div className="text-search-input-wrap">
            <input
              id="text-query-input"
              type="text"
              className="text-search-input"
              placeholder="Image name, project, XMP..."
              value={search.textQuery}
              onChange={(e) => search.setTextQuery(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') void search.runSearch()
              }}
              disabled={search.loading}
            />
            {search.textQuery && (
              <button
                type="button"
                className="btn-clear-text"
                onClick={() => search.setTextQuery('')}
                title="Clear text search"
                disabled={search.loading}
                aria-label="Clear text search"
              >
                <CloseIcon width="14" height="14" />
              </button>
            )}
          </div>
          <div className="search-mode-toggle" role="group" aria-label="Combine image and text search" aria-describedby="combine-mode-help">
            <button
              type="button"
              className={`toggle-chip ${search.combineMode === 'and' ? 'toggle-chip-active' : ''}`}
              onClick={() => search.setCombineMode('and')}
              aria-pressed={search.combineMode === 'and'}
              disabled={search.loading}
            >
              AND
            </button>
            <button
              type="button"
              className={`toggle-chip ${search.combineMode === 'or' ? 'toggle-chip-active' : ''}`}
              onClick={() => search.setCombineMode('or')}
              aria-pressed={search.combineMode === 'or'}
              disabled={search.loading}
            >
              OR
            </button>
          </div>
          <div className="muted text-search-help" id="combine-mode-help">
            {search.combineMode === 'and'
              ? 'Image + text: only results that match visually AND match the name/XMP.'
              : 'Image + text: visual and textual results are combined.'}
          </div>
        </div>

        <div className="controls">
          <label className="control">
            <span>
              Results <b>{search.k}</b>
            </span>
            <input
              type="range"
              min={1}
              max={30}
              step={1}
              value={search.k}
              disabled={search.loading}
              onChange={(e) => search.setK(Number(e.target.value))}
            />
          </label>
          <label className="control">
            <span>
              Min score <b>{Math.round(search.minScore * 100)}%</b>
            </span>
            <input
              type="range"
              min={0}
              max={0.95}
              step={0.05}
              value={search.minScore}
              disabled={search.loading}
              onChange={(e) => search.setMinScore(Number(e.target.value))}
            />
          </label>
        </div>

        <div className="search-action-bar">
          <button
            className="btn btn-primary search-action-btn"
            onClick={() => void search.runSearch()}
            disabled={(!search.file && !search.textQuery.trim()) || search.loading}
          >
            {search.loading ? 'Searching…' : 'Search'}
          </button>
        </div>

        {error && (
          <div className="error-box" role="alert" aria-live="polite">
            {error}
          </div>
        )}
        {search.error && (
          <div className="error-box" role="alert">
            {search.error}
          </div>
        )}
        {notice && (
          <div className="info-box" role="status">
            {notice}
          </div>
        )}
      </section>

      <section className="content">
        {search.loading && (
          <div className="busy-overlay" role="status" aria-live="polite">
            <span className="spinner" aria-hidden />
            Searching…
          </div>
        )}
        {search.response ? (
          <>
            <p className="result-meta muted" aria-live="polite">
              {search.response.results.length} of {search.response.total_indexed} indexed images
              {search.response.exact_match && ' · byte-identical match found'}
            </p>
            {search.response.results.length === 0 ? (
              <div className="info-box" role="status">
                {search.response.total_indexed === 0
                  ? 'Index is empty — initial scan may be running. Try again once indexing completes.'
                  : 'No matches above the score threshold.'}
              </div>
            ) : (
              <>
                <div className="browse-toolbar">
                  <ViewToggle mode={viewMode} onChange={setViewMode} />
                  <div className="browse-sort">
                    <label htmlFor="search-sort-select" className="muted browse-sort-label">Sort:</label>
                    <div className="browse-sort-controls">
                      <select
                        id="search-sort-select"
                        className="text-input browse-sort-select"
                        value={searchSort}
                        onChange={(e) => setSearchSort(e.target.value)}
                      >
                        {SEARCH_SORT_OPTIONS.map((opt) => (
                          <option key={opt.value} value={opt.value}>{opt.label}</option>
                        ))}
                      </select>
                      <button
                        type="button"
                        className="btn browse-sort-order-btn"
                        onClick={() => setSearchOrder((o) => (o === 'desc' ? 'asc' : 'desc'))}
                        title={`Current order: ${searchOrder}. Click to toggle.`}
                        aria-label={`Toggle sort direction, currently ${searchOrder === 'desc' ? 'descending' : 'ascending'}`}
                      >
                        {searchOrder === 'desc' ? <SortDescIcon width="16" height="16" /> : <SortAscIcon width="16" height="16" />}
                      </button>
                    </div>
                  </div>
                </div>
                <div className={search.selected ? 'split' : ''}>
                  {viewMode === 'grid' ? (
                    <ResultGrid
                      results={sortedResults}
                      selectedId={search.selected?.id ?? null}
                      onSelect={handleSelect}
                    />
                  ) : (
                    <ResultList
                      results={sortedResults}
                      selectedId={search.selected?.id ?? null}
                      onSelect={handleSelect}
                    />
                  )}
                  {search.selected && (
                    <DetailPanel result={search.selected} onClose={() => search.setSelected(null)} />
                  )}
                </div>
              </>
            )}
          </>
        ) : (
          <div className="placeholder">
            <p>Upload a query image and hit Search.</p>
            <p className="muted">
              Matches include exact copies, resized variants and visually similar renderings.
            </p>
          </div>
        )}
      </section>
    </main>
  )
}