import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError, getRescanDelta, getStats } from '../api'
import type { RescanDeltaResponse, Stats } from '../types'
import { useAuth } from '../features/auth/AuthContext'

const IDLE_REFRESH_MS = 10_000
const ACTIVE_SCAN_REFRESH_MS = 3_000
const STATS_TIMEOUT_MS = 10_000
// Exponential auto-retry for genuine outages: 2s, then 4s, then 8s. After the
// budget is exhausted polling falls back to the interval cadence + Retry button.
const NETWORK_BACKOFF_MS = [2_000, 4_000, 8_000]

/** Discriminator for the topbar banners.
 *  `network` = server genuinely unreachable (offline/timeout) — auto-retry.
 *  `server`  = server reachable but errored (429/5xx/other ApiError) — Retry.
 *  `auth`    = refresh cookie gone, session dead — Sign in again.
 */
export type StatsErrorKind = 'network' | 'server' | 'auth'

function classifyStatsError(err: unknown): StatsErrorKind {
  if (err instanceof ApiError) {
    // after withAuthRetry, any 401 means even the refresh cookie is gone
    if (err.status === 401) return 'auth'
    // 429 (rate-limited), 4xx (misconfig) and 5xx (server error) mean the
    // server IS reachable — just not succeeding on this request.
    return 'server'
  }
  // AbortError (timeout) / TypeError (fetch network failure) / anything else.
  return 'network'
}

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

export function useStatsPolling() {
  const { withAuthRetry } = useAuth()
  const [stats, setStats] = useState<Stats | null>(null)
  const [delta, setDelta] = useState<RescanDeltaResponse | null>(null)
  // `null` means "no error" / "no banner". See `classifyStatsError` for the
  // three kinds. Backoff timing lives in refs (not state) so interval ticks
  // don't inflate a retry counter or reschedule the exponential ramp.
  const [statsErrorKind, setStatsErrorKind] = useState<StatsErrorKind | null>(null)
  const inFlightRef = useRef(false)
  const backoffIdxRef = useRef(-1)
  const backoffTimerRef = useRef<number | null>(null)

  const clearBackoff = useCallback(() => {
    backoffIdxRef.current = -1
    if (backoffTimerRef.current != null) {
      window.clearTimeout(backoffTimerRef.current)
      backoffTimerRef.current = null
    }
  }, [])

  const refreshStats = useCallback(() => {
    // Skip duplicate polls: a slow tick/backoff request must not overlap the
    // interval tick, the manual Retry button, or another backoff attempt.
    if (inFlightRef.current) return
    inFlightRef.current = true

    withAuthRetry(() => withTimeout(getStats, STATS_TIMEOUT_MS, 'Cannot reach server. Please retry.'))
      .then((s) => {
        setStats(s)
        setStatsErrorKind(null)
        clearBackoff()
      })
      .catch((err: unknown) => {
        const kind = classifyStatsError(err)
        setStatsErrorKind(kind)
        // Auto-retry only genuine outages (offline/timeout). Auth failures
        // need a user gesture (logout), and server/rate-limit errors must not
        // be hammered faster than the interval cadence.
        if (kind === 'network') {
          if (backoffTimerRef.current != null) return
          const nextIdx = backoffIdxRef.current + 1
          if (nextIdx >= NETWORK_BACKOFF_MS.length) return
          backoffIdxRef.current = nextIdx
          backoffTimerRef.current = window.setTimeout(() => {
            backoffTimerRef.current = null
            refreshStats()
          }, NETWORK_BACKOFF_MS[nextIdx])
        }
      })
      .finally(() => {
        inFlightRef.current = false
      })

    withAuthRetry(() => withTimeout(getRescanDelta, STATS_TIMEOUT_MS, 'Failed to load scan changes.'))
      .then(setDelta)
      .catch(() => {})
  }, [withAuthRetry, clearBackoff])

  useEffect(refreshStats, [refreshStats])
  useEffect(() => {
    const t = setInterval(refreshStats, IDLE_REFRESH_MS)
    return () => clearInterval(t)
  }, [refreshStats])
  useEffect(() => {
    if (stats?.state !== 'scanning' && stats?.state !== 'paused') return
    const t = setInterval(refreshStats, ACTIVE_SCAN_REFRESH_MS)
    return () => clearInterval(t)
  }, [stats?.state, refreshStats])

  useEffect(() => {
    return () => {
      if (backoffTimerRef.current != null) window.clearTimeout(backoffTimerRef.current)
      inFlightRef.current = false
    }
  }, [])

  return { stats, delta, statsErrorKind, refreshStats, setStats, setDelta }
}