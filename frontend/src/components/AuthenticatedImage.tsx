import { useEffect, useRef, useState } from 'react'
import { authHeaders } from '../lib/authStorage'

interface Props extends Omit<React.ImgHTMLAttributes<HTMLImageElement>, 'src'> {
  src: string
}

// Global LRU blob cache — bounds memory at ~100×50KB ≈5MB per tab.
// Active images hold a refCount; eviction only revokes inactive entries.
const MAX_BLOB_CACHE = 100
const blobCache = new Map<string, { url: string; count: number }>()
const lruOrder: string[] = []

function touchLru(src: string) {
  const idx = lruOrder.indexOf(src)
  if (idx !== -1) lruOrder.splice(idx, 1)
  lruOrder.push(src)
}

function acquireCached(src: string): string | null {
  const entry = blobCache.get(src)
  if (!entry) return null
  entry.count += 1
  touchLru(src)
  return entry.url
}

function releaseCached(src: string, isCreator: boolean, createdUrl: string | null) {
  const entry = blobCache.get(src)
  if (entry) {
    entry.count = Math.max(0, entry.count - 1)
    // If we created this entry and still hold sole ref, keep it cached
    if (entry.count === 0 && isCreator && createdUrl) {
      // keep in cache for reuse, will be evicted LRU when over cap
    }
  } else if (isCreator && createdUrl) {
    // Should not happen: creator but no entry
    URL.revokeObjectURL(createdUrl)
  } else if (isCreator && createdUrl === null) {
    // no url created
  }
  evictIfNeeded()
}

function storeCached(src: string, url: string) {
  // If already stored (race), just bump count
  if (blobCache.has(src)) {
    // revoke duplicate
    URL.revokeObjectURL(url)
    return
  }
  blobCache.set(src, { url, count: 1 })
  lruOrder.push(src)
  evictIfNeeded()
}

function evictIfNeeded() {
  while (blobCache.size > MAX_BLOB_CACHE) {
    // Find oldest inactive entry
    let evictIdx = -1
    for (let i = 0; i < lruOrder.length; i++) {
      const key = lruOrder[i]
      const ent = blobCache.get(key)
      if (ent && ent.count === 0) { evictIdx = i; break }
    }
    if (evictIdx === -1) break // all active, cannot evict
    const key = lruOrder[evictIdx]
    const ent = blobCache.get(key)
    if (ent) URL.revokeObjectURL(ent.url)
    blobCache.delete(key)
    lruOrder.splice(evictIdx, 1)
  }
}

// Concurrency cap — prevents DoS under fast scroll (M2)
// 6 concurrent fetches matches browser per-host limit
let activeFetchCount = 0
const MAX_CONCURRENT = 6
const fetchQueue: Array<() => void> = []
function runWithConcurrency<T>(fn: () => Promise<T>): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const run = () => {
      activeFetchCount++
      fn()
        .then(resolve, reject)
        .finally(() => {
          activeFetchCount--
          const next = fetchQueue.shift()
          if (next) next()
        })
    }
    if (activeFetchCount < MAX_CONCURRENT) run()
    else fetchQueue.push(run)
  })
}

// Deduplicate concurrent fetches for same src
const pendingFetches = new Map<string, Promise<string>>()

// For tests: clear cache
export function __clearBlobCache() {
  for (const ent of blobCache.values()) URL.revokeObjectURL(ent.url)
  blobCache.clear()
  lruOrder.length = 0
  pendingFetches.clear()
  fetchQueue.length = 0
  activeFetchCount = 0
}

/**
 * Fetches an image with Authorization headers and renders it as a blob URL.
 * - Uses global LRU (100 entries) to bound objectURL memory
 * - AbortController per image + IntersectionObserver lazy (500px margin)
 * - Avoids leaking JWTs in query strings, Referer, logs, or browser history.
 */
export default function AuthenticatedImage({ src, ...rest }: Props) {
  const [objectUrl, setObjectUrl] = useState<string | null>(null)
  const placeholderRef = useRef<HTMLDivElement | null>(null)
  const [isVisible, setIsVisible] = useState(false)

  // IntersectionObserver lazy — defer fetch until near viewport
  useEffect(() => {
    // jsdom (tests) has no IntersectionObserver — treat as visible immediately
    if (typeof IntersectionObserver === 'undefined') {
      setIsVisible(true)
      return
    }
    const el = placeholderRef.current
    if (!el) {
      // ref not yet attached — retry observe shortly (keeps lazy, no eager visible)
      const retry = window.setTimeout(() => {
        const retryEl = placeholderRef.current
        if (retryEl) {
          const obs = new IntersectionObserver(
            (entries) => {
              for (const e of entries) {
                if (e.isIntersecting) {
                  setIsVisible(true)
                  obs.disconnect()
                  break
                }
              }
            },
            { rootMargin: '500px' },
          )
          obs.observe(retryEl)
        }
      }, 15)
      return () => window.clearTimeout(retry)
    }
    const obs = new IntersectionObserver(
      (entries) => {
        for (const e of entries) {
          if (e.isIntersecting) {
            setIsVisible(true)
            obs.disconnect()
            break
          }
        }
      },
      { rootMargin: '500px' },
    )
    obs.observe(el)
    return () => obs.disconnect()
  }, [src])

  useEffect(() => {
    // Don't fetch until visible (lazy)
    if (!isVisible) return

    let cancelled = false
    let isCreator = false
    let createdUrl: string | null = null
    const controller = new AbortController()

    const headers = authHeaders()
    const hasAuth = Object.keys(headers).length > 0

    if (!hasAuth) {
      setObjectUrl(src)
      return () => { cancelled = false }
    }

    // Try LRU cache first
    const cached = acquireCached(src)
    if (cached) {
      setObjectUrl(cached)
      return () => {
        releaseCached(src, false, null)
      }
    }

    // Bounded concurrency + dedup: avoid duplicate fetches for same src under fast scroll
    let pending = pendingFetches.get(src)
    if (pending) {
      pending
        .then((url) => {
          if (cancelled || controller.signal.aborted) return
          // pending url already stored, just acquire ref
          const cached = acquireCached(src)
          if (cached) setObjectUrl(cached)
          else setObjectUrl(url)
        })
        .catch(() => {
          if (!cancelled && !controller.signal.aborted) setObjectUrl(src)
        })
    } else {
      const fetchTask = () =>
        fetch(src, { headers, signal: controller.signal }).then(async (res) => {
          if (!res.ok) throw new Error(String(res.status))
          const blob = await res.blob()
          if (cancelled || controller.signal.aborted) throw new Error('cancelled')
          return URL.createObjectURL(blob)
        })
      const p = runWithConcurrency(fetchTask)
      pendingFetches.set(src, p)
      p.then((url) => {
        pendingFetches.delete(src)
        if (cancelled || controller.signal.aborted) {
          URL.revokeObjectURL(url)
          return
        }
        createdUrl = url
        isCreator = true
        storeCached(src, url)
        setObjectUrl(url)
      }).catch(() => {
        pendingFetches.delete(src)
        if (!cancelled && !controller.signal.aborted) setObjectUrl(src)
      })
    }

    return () => {
      cancelled = true
      controller.abort()
      // Release ref; if we created url but fetch hadn't stored yet, revoke directly
      if (isCreator && createdUrl) {
        // Already stored, just release
        releaseCached(src, true, createdUrl)
      } else if (!isCreator) {
        // We were using cached entry (if any) — decrement
        const entry = blobCache.get(src)
        if (entry) releaseCached(src, false, null)
        else if (createdUrl) URL.revokeObjectURL(createdUrl)
      }
    }
  }, [src, isVisible])

  // Show placeholder until visible+loaded to keep observer target mounted
  if (!isVisible) {
    return <div ref={placeholderRef} className="auth-image-placeholder" aria-hidden />
  }
  if (!objectUrl) return <div ref={placeholderRef} className="auth-image-placeholder" aria-hidden />
  return <img src={objectUrl} {...rest} />
}
