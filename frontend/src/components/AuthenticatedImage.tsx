import { useEffect, useState } from 'react'
import { authHeaders } from '../lib/authStorage'

interface Props extends Omit<React.ImgHTMLAttributes<HTMLImageElement>, 'src'> {
  src: string
}

/**
 * Fetches an image with Authorization headers and renders it as a blob URL.
 * Avoids leaking JWTs in query strings, Referer, logs, or browser history.
 * Falls back to plain <img src> if fetch fails (so <img src> with token still works).
 */
export default function AuthenticatedImage({ src, ...rest }: Props) {
  const [objectUrl, setObjectUrl] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    let blobUrl: string | null = null

    const headers = authHeaders()
    const hasAuth = Object.keys(headers).length > 0

    if (!hasAuth) {
      // No token — use plain URL (works in trusted-LAN / allow_unauth mode)
      setObjectUrl(src)
      return () => { cancelled = false }
    }

    fetch(src, { headers })
      .then(async (res) => {
        if (!res.ok) throw new Error(String(res.status))
        const blob = await res.blob()
        if (cancelled) return
        blobUrl = URL.createObjectURL(blob)
        setObjectUrl(blobUrl)
      })
      .catch(() => {
        if (!cancelled) setObjectUrl(src)
      })

    return () => {
      cancelled = true
      if (blobUrl) URL.revokeObjectURL(blobUrl)
    }
  }, [src])

  if (!objectUrl) return null
  return <img src={objectUrl} {...rest} />
}
