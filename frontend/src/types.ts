export interface SearchResult {
  id: number
  score: number
  exact: boolean
  source?: 'image' | 'text' | 'both'
  rel_path: string
  original_path: string
  width: number | null
  height: number | null
  xmp: Record<string, unknown>
  thumb_url: string
  file_url: string
}

export interface SearchResponse {
  total_indexed: number
  exact_match: boolean
  results: SearchResult[]
}

export type SearchCombineMode = 'and' | 'or'
export type AppPage = 'search' | 'settings' | 'browse'

export type ViewMode = 'grid' | 'list'
export type BrowseSort = 'indexed_at' | 'mtime' | 'size' | 'rel_path' | 'width' | 'height' | 'id'
export type BrowseOrder = 'asc' | 'desc'

export interface BrowseImage {
  id: number
  rel_path: string
  original_path: string
  size: number
  mtime: number
  sha256: string | null
  width: number | null
  height: number | null
  xmp: Record<string, unknown>
  indexed_at: string
  thumb_url: string
  file_url: string
}

export interface BrowseFilters {
  filename?: string
  q?: string
  ext?: string
  folder?: string
  xmp?: string
  xmp_query?: string
  size_min?: number
  size_max?: number
  width_min?: number
  width_max?: number
  height_min?: number
  height_max?: number
  indexed_from?: string
  indexed_to?: string
  mtime_from?: number
  mtime_to?: number
  has_xmp?: boolean
}

export interface BrowseResponse {
  items: BrowseImage[]
  total: number
  offset: number
  limit: number
  has_more: boolean
}

export interface ScanReport {
  trigger: string
  duration_sec: number
  seen: number
  processed: number
  added: number
  updated: number
  removed: number
  unchanged: number
  failed: number
  error_count?: number
  errors?: string[]
  elapsed_sec?: number
  scans_per_min?: number
  embeddings_per_min?: number
  scans_per_sec?: number
  embeddings_per_sec?: number
}

export interface RescanDeltaSummary {
  created_count: number
  deleted_count: number
  modified_count: number
  total_changes: number
}

export interface RescanDelta {
  timestamp: string
  summary: RescanDeltaSummary
  changes: {
    created: string[]
    deleted: string[]
    modified: string[]
  }
}

export interface RescanDeltaResponse {
  status: 'ok' | 'no_delta'
  timestamp?: string
  summary?: RescanDeltaSummary
  changes?: RescanDelta['changes']
  message?: string
}

export interface Stats {
  indexed: number
  db_count: number
  snapshot_image_count: number | null
  snapshot_age_sec: number | null
  snapshot_stale: boolean | null
  snapshot_max_age_hours: number
  delta_age_sec: number | null
  index_file_size_mb: number | null
  db_file_size_mb: number | null
  thumbs_count: number
  thumbs_size_mb: number | null
  thumbs_max_files: number
  state: string
  last_report: ScanReport | null
  inventory_source?: 'snapshot' | 'walk' | null
  last_scan: string | null
  model: string
  exiftool: boolean
  max_upload_mb: number
}
