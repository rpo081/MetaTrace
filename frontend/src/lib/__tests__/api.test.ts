import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { ApiError } from '../../api'
import { authenticatedUrl, clearAllTokens, setAccessToken, setAdminToken } from '../../lib/authStorage'

beforeEach(() => clearAllTokens())
afterEach(() => {
  vi.restoreAllMocks()
  clearAllTokens()
})

describe('ApiError', () => {
  it('carries status and message', () => {
    const e = new ApiError(404, 'not found')
    expect(e.status).toBe(404)
    expect(e.message).toBe('not found')
    expect(e.name).toBe('ApiError')
  })
})

describe('authenticatedUrl', () => {
  it('appends token with ? when no query', () => {
    setAccessToken('tok123')
    expect(authenticatedUrl('/api/thumb/1')).toBe('/api/thumb/1?token=tok123')
  })
  it('appends with & when query exists', () => {
    setAccessToken('tok123')
    expect(authenticatedUrl('/api/thumb/1?size=256')).toBe('/api/thumb/1?size=256&token=tok123')
  })
  it('encodes special chars', () => {
    setAccessToken('a b&c=')
    expect(authenticatedUrl('/api/thumb/1')).toBe('/api/thumb/1?token=a%20b%26c%3D')
  })
  it('returns plain url without token', () => {
    expect(authenticatedUrl('/api/thumb/1')).toBe('/api/thumb/1')
  })
  it('prefers access token over admin token', () => {
    setAccessToken('access123')
    setAdminToken('admin123')
    expect(authenticatedUrl('/api/thumb/1')).toBe('/api/thumb/1?token=access123')
  })
  it('falls back to admin token', () => {
    setAdminToken('admin123')
    expect(authenticatedUrl('/api/thumb/1')).toBe('/api/thumb/1?token=admin123')
  })
})

describe('ApiError mapping via api.ts', () => {
  it('maps 400 to friendly message', async () => {
    const { searchImage } = await import('../../api')
    globalThis.fetch = vi.fn().mockResolvedValue({ ok: false, status: 400, statusText: 'Bad Request', json: async () => ({ detail: 'bad' }) } as Response)
    await expect(searchImage(null, 10, 0, 'q')).rejects.toThrow(expect.objectContaining({ status: 400 }))
    try { await searchImage(null, 10, 0, 'q') } catch (e:any) { expect(e.message).toContain('Invalid request') }
  })
  it('maps 401 to authentication required', async () => {
    const { getStats } = await import('../../api')
    globalThis.fetch = vi.fn().mockResolvedValue({ ok: false, status: 401, statusText: 'Unauthorized', json: async () => ({ detail: 'unauth' }) } as Response)
    await expect(getStats()).rejects.toThrow(expect.objectContaining({ status: 401 }))
    try { await getStats() } catch (e:any) { expect(e.message).toMatch(/Authentication required/) }
  })
  it('maps 403, 404, 409, 413, 429, 500', async () => {
    const { getStats } = await import('../../api')
    const cases: Array<[number, RegExp]> = [
      [403, /Access denied/],
      [404, /not found/i],
      [409, /already running/],
      [413, /too large/i],
      [429, /Too many requests/],
      [500, /Server error/],
    ]
    for (const [code, pat] of cases) {
      globalThis.fetch = vi.fn().mockResolvedValue({ ok: false, status: code, statusText: 'Err', json: async () => ({ detail: 'x' }) } as Response)
      try { await getStats(); expect.fail('should throw') } catch (e:any) { expect(e.status).toBe(code); expect(e.message).toMatch(pat) }
    }
  })
  it('falls back to raw detail for unknown status', async () => {
    const { getStats } = await import('../../api')
    globalThis.fetch = vi.fn().mockResolvedValue({ ok: false, status: 418, statusText: "I'm a teapot", json: async () => ({ detail: 'teapot' }) } as Response)
    try { await getStats(); expect.fail('should throw') } catch (e:any) { expect(e.message).toBe('teapot') }
  })
  it('uses statusText when json has no detail', async () => {
    const { getStats } = await import('../../api')
    globalThis.fetch = vi.fn().mockResolvedValue({ ok: false, status: 418, statusText: "I'm a teapot", json: async () => ({}) } as Response)
    try { await getStats(); expect.fail('should throw') } catch (e:any) { expect(e.message).toBe("418 I'm a teapot") }
  })
})
