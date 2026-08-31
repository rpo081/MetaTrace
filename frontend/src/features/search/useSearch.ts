/** useSearch — encapsulates search state + runSearch lifecycle for SearchView.
 *
 * Owns: query image (file + previewUrl), text query, combine mode, k, minScore,
 * loading/error/notice, response, selected result, blob URL + abort lifecycles.
 * Exposes a single `state` object plus action callbacks so the view can render
 * without depending on the hook internals.
 */
import {
  useCallback,
  useEffect,
  useRef,
  useState,
} from 'react'
import type {
  SearchCombineMode,
  SearchResponse,
  SearchResult,
} from '../../types'
import { searchImage } from '../../api'

const SEARCH_TIMEOUT_MS = 30_000

function timeoutError(message: string): Error {
  const error = new Error(message)
  error.name = 'AbortError'
  return error
}

async function withTimeout<T>(
  run: (signal: AbortSignal) => Promise<T>,
  timeoutMs: number,
  message: string,
  parentSignal?: AbortSignal,
): Promise<T> {
  const controller = new AbortController()
  const timer = window.setTimeout(() => controller.abort(), timeoutMs)
  const onAbort = () => controller.abort()
  parentSignal?.addEventListener('abort', onAbort)
  try {
    const value = await run(controller.signal)
    if (controller.signal.aborted) throw timeoutError(message)
    return value
  } catch (error) {
    if (controller.signal.aborted && !parentSignal?.aborted) throw timeoutError(message)
    throw error
  } finally {
    window.clearTimeout(timer)
    parentSignal?.removeEventListener('abort', onAbort)
  }
}

export interface UseSearchOptions {
  /** Max upload size in MiB; used to pre-check client-side before submitting. */
  maxUploadMb?: number
  /** Called once a search settles (success, error, or superseded). Use to
   *  refresh stats without coupling this hook to the polling hook. */
  onSettled?: () => void
}

export interface UseSearchReturn {
  // query inputs
  file: File | null
  previewUrl: string | null
  textQuery: string
  combineMode: SearchCombineMode
  k: number
  minScore: number

  // result lifecycle
  loading: boolean
  error: string | null
  response: SearchResponse | null
  selected: SearchResult | null

  // actions
  onFile: (f: File) => void
  onClearFile: () => void
  setTextQuery: (s: string) => void
  setCombineMode: (m: SearchCombineMode) => void
  setK: (n: number) => void
  setMinScore: (n: number) => void
  runSearch: () => Promise<void>
  setSelected: (r: SearchResult | null) => void
}

export function useSearch(opts: UseSearchOptions = {}): UseSearchReturn {
  const { maxUploadMb, onSettled } = opts

  const [file, setFile] = useState<File | null>(null)
  const [previewUrl, setPreviewUrl] = useState<string | null>(null)
  const [textQuery, setTextQuery] = useState('')
  const [combineMode, setCombineMode] = useState<SearchCombineMode>('and')
  const [k, setK] = useState(5)
  const [minScore, setMinScore] = useState(0.0)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [response, setResponse] = useState<SearchResponse | null>(null)
  const [selected, setSelected] = useState<SearchResult | null>(null)

  const abortRef = useRef<AbortController | null>(null)
  const previewUrlRef = useRef<string | null>(null)
  const onSettledRef = useRef(onSettled)
  onSettledRef.current = onSettled

  // Track latest preview URL for unmount cleanup without re-registering the effect.
  useEffect(() => {
    previewUrlRef.current = previewUrl
  }, [previewUrl])

  useEffect(
    () => () => {
      abortRef.current?.abort()
      if (previewUrlRef.current) URL.revokeObjectURL(previewUrlRef.current)
    },
    [],
  )

  const onFile = useCallback(
    (f: File) => {
      // Client-side size pre-check; backend enforces the same limit (413).
      if (maxUploadMb != null && f.size > maxUploadMb * 1024 * 1024) {
        setError(`"${f.name}" exceeds the ${maxUploadMb} MiB upload limit.`)
        return
      }
      setError(null)
      setFile(f)
      if (previewUrl) URL.revokeObjectURL(previewUrl) // blob URL hygiene
      setPreviewUrl(URL.createObjectURL(f))
    },
    [maxUploadMb, previewUrl],
  )

  const onClearFile = useCallback(() => {
    setFile(null)
    if (previewUrl) {
      URL.revokeObjectURL(previewUrl)
      setPreviewUrl(null)
    }
  }, [previewUrl])

  const runSearch = useCallback(async () => {
    if (!file && !textQuery.trim()) return
    abortRef.current?.abort() // kill any in-flight search (stale-response race)
    const controller = new AbortController()
    abortRef.current = controller
    setLoading(true)
    setError(null)
    setSelected(null)
    try {
      const res = await withTimeout(
        (signal) => searchImage(file, k, minScore, textQuery, combineMode, signal),
        SEARCH_TIMEOUT_MS,
        'Search is taking longer than expected. Please try again.',
        controller.signal,
      )
      setResponse(res)
    } catch (e) {
      if (controller.signal.aborted || (e instanceof DOMException && e.name === 'AbortError')) {
        return // superseded by a newer search — leave state alone
      }
      setError(e instanceof Error ? e.message : String(e))
      setResponse(null)
    } finally {
      if (abortRef.current === controller) {
        setLoading(false)
        onSettledRef.current?.()
      }
    }
  }, [file, textQuery, combineMode, k, minScore])

  return {
    file,
    previewUrl,
    textQuery,
    combineMode,
    k,
    minScore,
    loading,
    error,
    response,
    selected,
    onFile,
    onClearFile,
    setTextQuery,
    setCombineMode,
    setK,
    setMinScore,
    runSearch,
    setSelected,
  }
}