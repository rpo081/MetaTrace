import { describe, expect, it } from 'vitest'
import { basename, fmtValue, formatBytes, formatDate, formatExt, sortedXmpEntries, xmpValue } from '../format'

describe('format utils', () => {
  it('basename splits on / and \\', () => {
    expect(basename('a/b/c.png')).toBe('c.png')
    expect(basename('C:\\Users\\a\\b.jpg')).toBe('b.jpg')
    expect(basename('noext')).toBe('noext')
  })
  it('fmtValue handles arrays and objects', () => {
    expect(fmtValue(['a', 'b'])).toBe('a; b')
    expect(fmtValue({ a: 1 })).toBe('{"a":1}')
    expect(fmtValue('hi')).toBe('hi')
  })
  it('formatBytes thresholds', () => {
    expect(formatBytes(500)).toBe('500 B')
    expect(formatBytes(2048)).toBe('2.0 KB')
    expect(formatBytes(2 * 1024 * 1024)).toBe('2.0 MB')
    expect(formatBytes(3 * 1024 * 1024 * 1024)).toBe('3.0 GB')
  })
  it('formatExt uppercases', () => {
    expect(formatExt('a.jpg')).toBe('JPG')
    expect(formatExt('noext')).toBe('')
  })
  it('formatDate handles number and string', () => {
    expect(formatDate(0)).toMatch(/\d{4}/)
    expect(formatDate('2026-01-01T00:00:00Z')).toMatch(/\d{4}/)
    expect(formatDate(undefined)).toBeNull()
    expect(formatDate('not-a-date')).toBeNull()
  })
  it('xmpValue case-insensitive tail match', () => {
    expect(xmpValue({ 'dc:Creator': 'Alice' }, 'Creator')).toBe('Alice')
    expect(xmpValue({ 'Creator': 'Bob' }, 'creator')).toBe('Bob')
    expect(xmpValue({}, 'Creator')).toBeNull()
  })
  it('sortedXmpEntries prioritizes Creator/Description', () => {
    const xmp = { 'x:Other': '1', 'dc:Creator': 'A', 'dc:Description': 'D' }
    const sorted = sortedXmpEntries(xmp)
    expect(sorted[0][0]).toBe('Creator')
    expect(sorted[1][0]).toBe('Description')
  })
})
