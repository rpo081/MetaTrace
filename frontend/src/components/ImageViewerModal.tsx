import { useEffect, useRef, useState } from 'react'
import type { BrowseImage, SearchResult } from '../types'
import { basename, formatDims, formatExt } from '../utils/format'
import { getSignedFileUrl } from '../features/auth/api'
import { authHeaders } from '../lib/authStorage'
import AuthenticatedImage from './AuthenticatedImage'
import {
  ChevronLeftIcon,
  ChevronRightIcon,
  CloseIcon,
  ExternalLinkIcon,
  FitIcon,
  MinusIcon,
  PlusIcon,
  ResetIcon,
} from './Icon'

export type ViewerImage = SearchResult | BrowseImage

interface Props {
  open: boolean
  images: ViewerImage[]
  index: number
  onClose: () => void
  onNavigate: (nextIndex: number) => void
}

type LoadStatus = 'loading' | 'ready' | 'error'

const MIN_SCALE = 0.25
const MAX_SCALE = 8
const STEP_FACTOR = 1.25
const WHEEL_FACTOR = 1.1
const PAN_STEP = 40
const DBLCLICK_SCALE = 2

/** Clamp a scale into the free-zoom range 25–800 %. */
function clampScale(s: number): number {
  return Math.min(MAX_SCALE, Math.max(MIN_SCALE, s))
}

export default function ImageViewerModal({ open, images, index, onClose, onNavigate }: Props) {
  const [status, setStatus] = useState<LoadStatus>('loading')
  const [objectUrl, setObjectUrl] = useState<string | null>(null)
  const [zoomPct, setZoomPct] = useState(100)
  const [retryNonce, setRetryNonce] = useState(0)

  const dialogRef = useRef<HTMLDivElement | null>(null)
  const closeRef = useRef<HTMLButtonElement | null>(null)
  const retryRef = useRef<HTMLButtonElement | null>(null)
  const returnFocusRef = useRef<HTMLElement | null>(null)
  const stageRef = useRef<HTMLDivElement | null>(null)
  const imgRef = useRef<HTMLImageElement | null>(null)
  const urlRef = useRef<string | null>(null)

  // View transform lives in refs; the <img> transform is applied imperatively
  // via imgRef (never via a style prop — strict CSP style-src 'self').
  const scaleRef = useRef(1)
  const fitScaleRef = useRef(1)
  const panRef = useRef({ x: 0, y: 0 })
  const dragRef = useRef<{
    pointerId: number
    startX: number
    startY: number
    panX: number
    panY: number
  } | null>(null)
  const pinchRef = useRef<{
    idA: number
    idB: number
    startDist: number
    startScale: number
  } | null>(null)
  const pointersRef = useRef(new Map<number, { x: number; y: number }>())

  // Latest callbacks/values for the [open]-only chrome effect (same pattern as
  // AddUserModal: onCancel in a ref, effect deps only [open]).
  const onCloseRef = useRef(onClose)
  const onNavigateRef = useRef(onNavigate)
  useEffect(() => {
    onCloseRef.current = onClose
  }, [onClose])
  useEffect(() => {
    onNavigateRef.current = onNavigate
  }, [onNavigate])
  const indexRef = useRef(index)
  indexRef.current = index
  const countRef = useRef(images.length)
  countRef.current = images.length

  const current: ViewerImage | undefined = open ? images[index] : undefined

  function applyTransform(): void {
    const img = imgRef.current
    if (!img) return
    img.style.transformOrigin = 'center center'
    img.style.transform = `translate(${panRef.current.x}px, ${panRef.current.y}px) scale(${scaleRef.current})`
  }

  function stageCenter(): { x: number; y: number } | null {
    const stage = stageRef.current
    if (!stage) return null
    const r = stage.getBoundingClientRect()
    return { x: r.left + r.width / 2, y: r.top + r.height / 2 }
  }

  function setScaleAndPan(nextScale: number, nextPan: { x: number; y: number }): void {
    scaleRef.current = clampScale(nextScale)
    panRef.current = nextPan
    setZoomPct(Math.round(scaleRef.current * 100))
    applyTransform()
  }

  /** Zoom by factor keeping the stage point (clientX/clientY) fixed. */
  function zoomAt(clientX: number, clientY: number, factor: number): void {
    const c = stageCenter()
    if (!c) return
    const s = scaleRef.current
    const ns = clampScale(s * factor)
    if (ns === s) return
    const k = 1 - ns / s
    setScaleAndPan(ns, {
      x: panRef.current.x + (clientX - c.x - panRef.current.x) * k,
      y: panRef.current.y + (clientY - c.y - panRef.current.y) * k,
    })
  }

  function zoomCentered(factor: number): void {
    const c = stageCenter()
    if (!c) return
    zoomAt(c.x, c.y, factor)
  }

  function zoomTo(target: number): void {
    const s = scaleRef.current
    if (s === 0) return
    zoomCentered(target / s)
  }

  function computeFitScale(): number {
    const stage = stageRef.current
    const img = imgRef.current
    if (!stage || !img || !img.naturalWidth || !img.naturalHeight) return fitScaleRef.current
    const sw = stage.clientWidth
    const sh = stage.clientHeight
    if (!sw || !sh) return fitScaleRef.current
    return clampScale(Math.min(sw / img.naturalWidth, sh / img.naturalHeight))
  }

  function goFit(): void {
    const fit = computeFitScale()
    fitScaleRef.current = fit
    setScaleAndPan(fit, { x: 0, y: 0 })
  }

  function goOneToOne(): void {
    zoomTo(1)
  }

  function recenter(): void {
    panRef.current = { x: 0, y: 0 }
    applyTransform()
  }

  function panBy(dx: number, dy: number): void {
    const nx = Math.min(4096, Math.max(-4096, panRef.current.x + dx))
    const ny = Math.min(4096, Math.max(-4096, panRef.current.y + dy))
    panRef.current = { x: nx, y: ny }
    applyTransform()
  }

  function toggleFitZoom(): void {
    const fit = fitScaleRef.current
    if (Math.abs(scaleRef.current - fit) < 0.001) zoomTo(DBLCLICK_SCALE)
    else goFit()
  }

  function goPrev(): void {
    const i = indexRef.current
    if (i > 0) onNavigateRef.current(i - 1)
  }

  function goNext(): void {
    const i = indexRef.current
    if (i < countRef.current - 1) onNavigateRef.current(i + 1)
  }

  async function openOriginalInNewTab(): Promise<void> {
    const item = images[indexRef.current]
    if (!item) return
    try {
      const url = await getSignedFileUrl(item.id)
      window.open(url, '_blank', 'noopener,noreferrer')
    } catch {
      window.open(item.file_url, '_blank', 'noopener,noreferrer')
    }
  }

  // Fresh closures for the chrome effect without re-subscribing it.
  const actionsRef = useRef({
    zoomIn: () => {},
    zoomOut: () => {},
    goFit: () => {},
    goOneToOne: () => {},
    recenter: () => {},
    panBy: (_dx: number, _dy: number) => {},
    goPrev: () => {},
    goNext: () => {},
  })
  actionsRef.current = {
    zoomIn: () => zoomCentered(STEP_FACTOR),
    zoomOut: () => zoomCentered(1 / STEP_FACTOR),
    goFit,
    goOneToOne,
    recenter,
    panBy,
    goPrev,
    goNext,
  }

  // --- Modal chrome: focus mgmt, scroll lock, ESC + tab trap + shortcuts ---
  useEffect(() => {
    if (!open) return
    returnFocusRef.current =
      document.activeElement instanceof HTMLElement ? document.activeElement : null

    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    // Focus the Close button on open (stable target — no focus steal on polls).
    closeRef.current?.focus()

    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault()
        onCloseRef.current()
        return
      }
      if (e.key === 'Tab') {
        const focusables = dialogRef.current?.querySelectorAll<HTMLElement>(
          'button:not([disabled]), input:not([disabled]), [href], select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
        )
        if (!focusables || focusables.length === 0) return
        const first = focusables[0]
        const last = focusables[focusables.length - 1]
        const active = document.activeElement
        if (e.shiftKey && active === first) {
          e.preventDefault()
          last.focus()
        } else if (!e.shiftKey && active === last) {
          e.preventDefault()
          first.focus()
        }
        return
      }
      const a = actionsRef.current
      switch (e.key) {
        case '+':
        case '=':
          e.preventDefault()
          a.zoomIn()
          break
        case '-':
        case '_':
          e.preventDefault()
          a.zoomOut()
          break
        case '0':
          e.preventDefault()
          a.goFit()
          break
        case '1':
          e.preventDefault()
          a.goOneToOne()
          break
        case 'Home':
          e.preventDefault()
          a.recenter()
          break
        case 'ArrowLeft':
          e.preventDefault()
          if (e.ctrlKey || e.metaKey) a.panBy(PAN_STEP, 0)
          else if (countRef.current > 1) a.goPrev()
          else a.panBy(PAN_STEP, 0)
          break
        case 'ArrowRight':
          e.preventDefault()
          if (e.ctrlKey || e.metaKey) a.panBy(-PAN_STEP, 0)
          else if (countRef.current > 1) a.goNext()
          else a.panBy(-PAN_STEP, 0)
          break
        case 'ArrowUp':
          e.preventDefault()
          a.panBy(0, PAN_STEP)
          break
        case 'ArrowDown':
          e.preventDefault()
          a.panBy(0, -PAN_STEP)
          break
        default:
          break
      }
    }
    document.addEventListener('keydown', onKeyDown)

    // Wheel = zoom to cursor (±10 %). Native non-passive listener so
    // preventDefault works (React onWheel may be passive at root).
    const stage = stageRef.current
    const onWheel = (e: WheelEvent) => {
      e.preventDefault()
      const c = stageCenter()
      if (!c) return
      zoomAt(e.clientX, e.clientY, e.deltaY < 0 ? WHEEL_FACTOR : 1 / WHEEL_FACTOR)
    }
    stage?.addEventListener('wheel', onWheel, { passive: false })

    return () => {
      document.body.style.overflow = previousOverflow
      document.removeEventListener('keydown', onKeyDown)
      stage?.removeEventListener('wheel', onWheel)
      returnFocusRef.current?.focus()
      returnFocusRef.current = null
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])

  // --- Original loading: signed URL -> fetch with auth -> blob object URL ---
  useEffect(() => {
    if (!open) return
    const item = images[index]
    if (!item) return
    let cancelled = false
    // Reset zoom/pan to Fit for the incoming image; the exact fit scale is
    // measured again once the <img> fires onLoad.
    panRef.current = { x: 0, y: 0 }
    scaleRef.current = fitScaleRef.current
    setZoomPct(Math.round(fitScaleRef.current * 100))
    if (urlRef.current) {
      URL.revokeObjectURL(urlRef.current)
      urlRef.current = null
    }
    setObjectUrl(null)
    setStatus('loading')
    ;(async () => {
      try {
        const signed = await getSignedFileUrl(item.id)
        if (cancelled) return
        const res = await fetch(signed, { headers: authHeaders(), credentials: 'include' })
        if (!res.ok) throw new Error(`fetch failed: ${res.status}`)
        const blob = await res.blob()
        if (cancelled) return
        const url = URL.createObjectURL(blob)
        if (cancelled) {
          URL.revokeObjectURL(url)
          return
        }
        urlRef.current = url
        setObjectUrl(url)
        // status flips to ready in the <img> onLoad handler (thumb underlay
        // stays visible until then).
      } catch {
        if (!cancelled) setStatus('error')
      }
    })()
    return () => {
      cancelled = true
      if (urlRef.current) {
        URL.revokeObjectURL(urlRef.current)
        urlRef.current = null
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, index, retryNonce])

  // Focus the retry button when loading fails.
  useEffect(() => {
    if (open && status === 'error') retryRef.current?.focus()
  }, [open, status])

  if (!open || !current) return null

  const name = basename(current.rel_path)
  const dims = formatDims(current.width, current.height)
  const ext = formatExt(current.rel_path)
  const metaParts = [dims, ext].filter(Boolean)
  const hasMultiple = images.length > 1

  const handleMainLoad = () => {
    const fit = computeFitScale()
    fitScaleRef.current = fit
    panRef.current = { x: 0, y: 0 }
    scaleRef.current = fit
    setZoomPct(Math.round(fit * 100))
    setStatus('ready')
    applyTransform()
  }

  const onStagePointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    if (status !== 'ready') return
    if (e.pointerType === 'mouse' && e.button !== 0) return
    const stage = stageRef.current
    pointersRef.current.set(e.pointerId, { x: e.clientX, y: e.clientY })
    if (pointersRef.current.size === 2) {
      // Begin pinch: capture midpoint + distance + start scale.
      const [pA, pB] = [...pointersRef.current.values()]
      const dist = Math.hypot(pA.x - pB.x, pA.y - pB.y)
      const ids = [...pointersRef.current.keys()]
      pinchRef.current = { idA: ids[0], idB: ids[1], startDist: dist || 1, startScale: scaleRef.current }
      dragRef.current = null
      try {
        stage?.setPointerCapture(e.pointerId)
      } catch {
        /* ignore */
      }
      return
    }
    dragRef.current = {
      pointerId: e.pointerId,
      startX: e.clientX,
      startY: e.clientY,
      panX: panRef.current.x,
      panY: panRef.current.y,
    }
    stage?.classList.add('viewer-panning')
    try {
      stage?.setPointerCapture(e.pointerId)
    } catch {
      /* ignore */
    }
  }

  const onStagePointerMove = (e: React.PointerEvent<HTMLDivElement>) => {
    if (!pointersRef.current.has(e.pointerId)) return
    pointersRef.current.set(e.pointerId, { x: e.clientX, y: e.clientY })
    const pinch = pinchRef.current
    if (pinch && pointersRef.current.size >= 2) {
      // Pinch = zoom to midpoint.
      const pA = pointersRef.current.get(pinch.idA)
      const pB = pointersRef.current.get(pinch.idB)
      if (!pA || !pB) return
      const dist = Math.hypot(pA.x - pB.x, pA.y - pB.y) || 1
      const midX = (pA.x + pB.x) / 2
      const midY = (pA.y + pB.y) / 2
      const c = stageCenter()
      if (!c) return
      const ns = clampScale((pinch.startScale * dist) / pinch.startDist)
      const s = scaleRef.current
      if (ns !== s) {
        const k = 1 - ns / s
        setScaleAndPan(ns, {
          x: panRef.current.x + (midX - c.x - panRef.current.x) * k,
          y: panRef.current.y + (midY - c.y - panRef.current.y) * k,
        })
      }
      return
    }
    const drag = dragRef.current
    if (!drag || drag.pointerId !== e.pointerId) return
    panRef.current = {
      x: Math.min(4096, Math.max(-4096, drag.panX + (e.clientX - drag.startX))),
      y: Math.min(4096, Math.max(-4096, drag.panY + (e.clientY - drag.startY))),
    }
    applyTransform()
  }

  const endPointer = (e: React.PointerEvent<HTMLDivElement>) => {
    pointersRef.current.delete(e.pointerId)
    if (pinchRef.current && pointersRef.current.size < 2) pinchRef.current = null
    if (dragRef.current?.pointerId === e.pointerId) dragRef.current = null
    if (pointersRef.current.size === 0) stageRef.current?.classList.remove('viewer-panning')
  }

  return (
    <div
      className="modal-backdrop"
      role="presentation"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onCloseRef.current()
      }}
    >
      <div
        ref={dialogRef}
        className="modal-card modal-card-viewer"
        role="dialog"
        aria-modal="true"
        aria-labelledby="image-viewer-title"
      >
        <div className="viewer-header">
          <h2 id="image-viewer-title" className="viewer-title" title={current.original_path}>
            {name}
            {metaParts.length > 0 && <span className="viewer-meta muted"> · {metaParts.join(' · ')}</span>}
          </h2>
          <span className="viewer-counter muted" aria-live="polite">
            {hasMultiple ? `${index + 1} von ${images.length}` : '1 von 1'}
          </span>
          <button
            ref={closeRef}
            type="button"
            className="btn btn-ghost btn-icon"
            onClick={() => onCloseRef.current()}
            aria-label="Schließen"
          >
            <CloseIcon width="18" height="18" />
          </button>
        </div>

        <div
          ref={stageRef}
          className="viewer-stage"
          onPointerDown={onStagePointerDown}
          onPointerMove={onStagePointerMove}
          onPointerUp={endPointer}
          onPointerCancel={endPointer}
        >
          {status !== 'ready' && (
            <AuthenticatedImage src={current.thumb_url} className="viewer-thumb" alt="" />
          )}
          {status === 'loading' && !objectUrl && (
            <div className="viewer-loading" role="status" aria-live="polite">
              <span className="spinner" aria-hidden />
              Original wird geladen…
            </div>
          )}
          {status === 'error' && (
            <div className="error-box viewer-error" role="alert">
              Das Original konnte nicht geladen werden.
              <button
                ref={retryRef}
                type="button"
                className="btn btn-sm"
                onClick={() => setRetryNonce((n) => n + 1)}
              >
                Erneut versuchen
              </button>
            </div>
          )}
          {objectUrl && status !== 'error' && (
            <img
              ref={imgRef}
              src={objectUrl}
              alt={name}
              className="viewer-img"
              draggable={false}
              onLoad={handleMainLoad}
              onError={() => setStatus('error')}
              onDoubleClick={toggleFitZoom}
            />
          )}
        </div>

        <div className="viewer-toolbar" role="toolbar" aria-label="Bildansicht-Steuerung">
          <button
            type="button"
            className="btn viewer-btn"
            onClick={goPrev}
            disabled={!hasMultiple || index === 0}
            aria-label="Vorheriges Bild"
          >
            <ChevronLeftIcon width="18" height="18" />
          </button>
          <button
            type="button"
            className="btn viewer-btn"
            onClick={goNext}
            disabled={!hasMultiple || index === images.length - 1}
            aria-label="Nächstes Bild"
          >
            <ChevronRightIcon width="18" height="18" />
          </button>
          <span className="viewer-toolbar-sep" aria-hidden="true" />
          <button
            type="button"
            className="btn viewer-btn"
            onClick={() => actionsRef.current.zoomOut()}
            aria-label="Verkleinern"
          >
            <MinusIcon width="18" height="18" />
          </button>
          <span className="viewer-zoom-label" aria-live="polite" aria-label="Zoomstufe">
            {zoomPct} %
          </span>
          <button
            type="button"
            className="btn viewer-btn"
            onClick={() => actionsRef.current.zoomIn()}
            aria-label="Vergrößern"
          >
            <PlusIcon width="18" height="18" />
          </button>
          <span className="viewer-toolbar-sep" aria-hidden="true" />
          <button
            type="button"
            className="btn viewer-btn"
            onClick={goFit}
            aria-label="Zoom zurücksetzen"
            title="Zoom zurücksetzen (Fit)"
          >
            <ResetIcon width="18" height="18" />
          </button>
          <button
            type="button"
            className="btn viewer-btn"
            onClick={goFit}
            aria-label="Einpassen"
            title="Bild einpassen"
          >
            <FitIcon width="18" height="18" />
          </button>
          <button type="button" className="btn viewer-btn-text" onClick={goOneToOne} aria-label="Originalgröße (1:1)">
            1:1
          </button>
          <button
            type="button"
            className="btn viewer-btn"
            onClick={() => void openOriginalInNewTab()}
            aria-label="Original in neuem Tab öffnen"
            title="Original in neuem Tab öffnen"
          >
            <ExternalLinkIcon width="18" height="18" />
          </button>
        </div>

        <div className="viewer-hint muted">
          <span>
            <kbd>Doppelklick</kbd> 200 %
          </span>
          <span>
            <kbd>Scrollen</kbd> Zoom
          </span>
          <span>
            <kbd>Ziehen</kbd> Verschieben
          </span>
          <span>
            <kbd>+</kbd>/<kbd>−</kbd> Zoom
          </span>
          <span>
            <kbd>0</kbd> Einpassen
          </span>
          <span>
            <kbd>1</kbd> 1:1
          </span>
          <span>
            <kbd>←</kbd>/<kbd>→</kbd> Blättern
          </span>
          <span>
            <kbd>Esc</kbd> Schließen
          </span>
        </div>
      </div>
    </div>
  )
}
