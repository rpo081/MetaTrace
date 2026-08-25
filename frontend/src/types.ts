export interface SearchResult {
  id: number
  score: number
  exact: boolean
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
  state: string
  last_report: ScanReport | null
  inventory_source?: 'snapshot' | 'walk' | null
  last_scan: string | null
  model: string
  exiftool: boolean
  /** Upload limit in MiB; optional until the backend exposes it in /api/stats. */
  max_upload_mb?: number
}
