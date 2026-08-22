import { useCallback, useRef, useState } from 'react'
import type { KeyboardEvent } from 'react'

interface Props {
  previewUrl: string | null
  onFile: (file: File) => void
  disabled?: boolean
}

export default function Dropzone({ previewUrl, onFile, disabled }: Props) {
  const [over, setOver] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)

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
      if (e.key === 'Enter' || e.key === ' ' || e.key === 'Spacebar') {
        e.preventDefault()
        openPicker()
      }
    },
    [openPicker],
  )

  return (
    <div
      className={`dropzone ${over ? 'dropzone-over' : ''}`}
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
      aria-label="Upload query image"
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
        <img src={previewUrl} alt="query" className="query-preview" />
      ) : (
        <div className="dropzone-hint">
          <div className="dropzone-icon" aria-hidden>
            ⌕
          </div>
          <p>Drop an image here or click to browse</p>
          <p className="muted">JPG · PNG · PSD</p>
        </div>
      )}
    </div>
  )
}
