/** Shared formatting helpers (single source of truth). */

export function basename(p: string): string {
  return p.split(/[\\/]/).pop() ?? p
}

export function fmtValue(v: unknown): string {
  if (Array.isArray(v)) return v.join('; ')
  if (typeof v === 'object' && v !== null) return JSON.stringify(v)
  return String(v)
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(1)} GB`
}

export function formatDims(width: number | null | undefined, height: number | null | undefined): string | null {
  if (width != null && height != null) return `${width}×${height}`
  return null
}

export function formatExt(p: string): string {
  const dot = p.lastIndexOf('.')
  return dot >= 0 ? p.slice(dot + 1).toUpperCase() : ''
}

export function formatDate(val: string | number | undefined): string | null {
  if (val == null) return null
  const d = new Date(typeof val === 'number' ? val * 1000 : val)
  if (isNaN(d.getTime())) return null
  return d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' })
}

export function xmpValue(xmp: Record<string, unknown> | undefined, attribute: string): string | null {
  if (!xmp) return null
  const attrLower = attribute.toLowerCase()
  for (const [key, value] of Object.entries(xmp)) {
    const tail = key.toLowerCase().split(':').pop() ?? key.toLowerCase()
    if (tail === attrLower) return fmtValue(value)
  }
  return null
}

export function matchesXmpAttribute(key: string, attribute: string): boolean {
  const keyLower = key.toLowerCase()
  const attrLower = attribute.toLowerCase()
  if (keyLower.endsWith(attrLower)) return true
  const tail = keyLower.split(':').pop() ?? keyLower
  return tail === attrLower
}

export function sortedXmpEntries(
  xmp: Record<string, unknown>,
  priority: readonly string[] = ['Creator', 'Description'],
): Array<[string, unknown]> {
  const entries = Object.entries(xmp)
  if (entries.length === 0) return []
  const prioritized: Array<[string, unknown]> = []
  const rest = [...entries]
  for (const attr of priority) {
    const idx = rest.findIndex(([key]) => matchesXmpAttribute(key, attr))
    if (idx !== -1) {
      const [k, v] = rest.splice(idx, 1)[0]
      prioritized.push([attr, v])
    }
  }
  return [...prioritized, ...rest]
}
