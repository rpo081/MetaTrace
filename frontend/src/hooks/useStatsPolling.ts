import { useCallback, useEffect, useState } from 'react'
import { getRescanDelta, getStats } from '../api'
import type { RescanDeltaResponse, Stats } from '../types'

const IDLE_REFRESH_MS = 10_000
const ACTIVE_SCAN_REFRESH_MS = 2_000
const STATS_TIMEOUT_MS = 10_000

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
  const [stats, setStats] = useState<Stats | null>(null)
  const [delta, setDelta] = useState<RescanDeltaResponse | null>(null)
  const [statsError, setStatsError] = useState(false)
  const [statsRetryCount, setStatsRetryCount] = useState(0)

  const refreshStats = useCallback(() => {
    withTimeout(getStats, STATS_TIMEOUT_MS, 'Cannot reach server. Please retry.')
      .then((s) => {
        setStats(s)
        setStatsError(false)
        setStatsRetryCount(0)
      })
      .catch(() => {
        setStatsError(true)
        setStatsRetryCount((c) => c + 1)
      })
    withTimeout(getRescanDelta, STATS_TIMEOUT_MS, 'Failed to load scan changes.')
      .then(setDelta)
      .catch(() => {})
  }, [])

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
    if (!statsError || statsRetryCount >= 3) return
    const delay = Math.min(2 ** (statsRetryCount - 1) * 2_000, 8_000)
    const t = window.setTimeout(refreshStats, delay)
    return () => window.clearTimeout(t)
  }, [statsError, statsRetryCount, refreshStats])

  return { stats, delta, statsError, statsRetryCount, refreshStats, setStats, setDelta }
}
