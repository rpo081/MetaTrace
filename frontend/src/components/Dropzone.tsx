import { useCallback, useEffect, useRef, useState } from 'react'
import type { KeyboardEvent } from 'react'
import { SearchIcon, CloseIcon } from './Icon'

interface Props {
  previewUrl: string | null
  onFile: (file: File) => void
  onClear?: () => void
  disabled?: boolean
}

function mimeToExt(mime: string): string {
  const sub = mime.split('/')[1]?.toLowerCase() ?? ''
  if (sub === 'jpeg' || sub === 'jpg') return 'jpg'
  if (sub === 'png') return 'png'
  if (sub === 'webp') return 'webp'
  if (sub === 'gif') return 'gif'
  if (sub === 'tiff' || sub === 'tif') return 'tif'
  return sub || 'png'
}

function isTextInputActive(): boolean {
  const el = document.activeElement
  if (!el || !(el instanceof HTMLElement)) return false
  const tag = el.tagName
  return tag === 'INPUT' || tag === 'TEXTAREA' || el.isContentEditable
}

export default function Dropzone({ previewUrl, onFile, onClear, disabled }: Props) {
  const [over, setOver] = useState(false)
  const [pasteFlash, setPasteFlash] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)
  const onFileRef = useRef(onFile)
  const disabledRef = useRef(disabled)

  onFileRef.current = onFile
  disabledRef.current = disabled

  useEffect(() => {
    const handlePaste = (e: ClipboardEvent) => {
      if (disabledRef.current || isTextInputActive()) return
      const items = e.clipboardData?.items
      if (!items) return
      for (let i = 0; i < items.length; i++) {
        const item = items[i]
        if (item.type.startsWith('image/')) {
          const blob = item.getAsFile()
          if (!blob) continue
          e.preventDefault()
          const ext = mimeToExt(blob.type)
          const file = new File([blob], `pasted-image.${ext}`, { type: blob.type })
          onFileRef.current(file)
          setPasteFlash(true)
          window.setTimeout(() => setPasteFlash(false), 600)
          return
        }
      }
    }
    document.addEventListener('paste', handlePaste)
    return () => document.removeEventListener('paste', handlePaste)
  }, [])

  const openPicker = useCallback(() => {
    if (!disabled) inputRef.current?.click()
  }, [disabled])

  const handleFiles = useCallback(
    (files: FileList | null) => {
      const file = files?.[0]
      if (file) onFile(file)
    },
    [onFile],
  )

  const onKeyDown = useCallback(
    (e: KeyboardEvent<HTMLDivElement>) => {
      if (e.target !== e.currentTarget) {
        return
      }
      if (e.key === 'Enter' || e.key === ' ' || e.key === 'Spacebar') {
        e.preventDefault()
        openPicker()
        return
      }
      if (e.key === 'Escape') {
        e.preventDefault()
        setOver(false)
      }
    },
    [openPicker],
  )

  return (
    <div
      className={`dropzone ${over ? 'dropzone-over' : ''} ${pasteFlash ? 'dropzone-paste-flash' : ''}`}
      onClick={openPicker}
      onKeyDown={onKeyDown}
      onDragOver={(e) => {
        e.preventDefault()
        setOver(true)
      }}
      onDragLeave={() => setOver(false)}
      onDrop={(e) => {
        e.preventDefault()
        setOver(false)
        if (!disabled) handleFiles(e.dataTransfer.files)
      }}
      role="button"
      tabIndex={disabled ? -1 : 0}
      aria-disabled={disabled || undefined}
      aria-label="Upload query image — drop, paste, or click to browse"
    >
      <input
        ref={inputRef}
        type="file"
        accept="image/*,.psd"
        hidden
        onChange={(e) => {
          handleFiles(e.target.files)
          e.target.value = ''
        }}
      />
      {previewUrl ? (
        <div className="preview-wrap">
          <img src={previewUrl} alt="query" className="query-preview" />
          {onClear && (
            <button
              type="button"
              className="btn-clear-image"
              onClick={(e) => {
                e.stopPropagation()
                onClear()
              }}
              onKeyDown={(e) => {
                e.stopPropagation()
              }}
              title="Remove query image"
              disabled={disabled}
              aria-label="Remove query image"
            >
              <CloseIcon width="14" height="14" />
            </button>
          )}
        </div>
      ) : (
        <div className="dropzone-hint">
          <div className="dropzone-icon" aria-hidden>
            <SearchIcon width="40" height="40" />
          </div>
          <p>Drop, paste, or click to browse</p>
          <p className="muted">JPG · PNG · PSD · TIF</p>
          <p className="muted dropzone-shortcut-hint" aria-hidden>
            <kbd>⌘V</kbd> / <kbd>Ctrl+V</kbd>
          </p>
        </div>
      )}
    </div>
  )
}
