import { beforeEach, describe, expect, it, vi, afterEach } from 'vitest'
import { render, waitFor } from '@testing-library/react'
import AuthenticatedImage from '../AuthenticatedImage'
import { clearAllTokens } from '../../lib/authStorage'

beforeEach(() => clearAllTokens())
afterEach(() => {
  vi.restoreAllMocks()
  clearAllTokens()
})

describe('AuthenticatedImage', () => {
  it('fetches with auth header when token present', async () => {
    localStorage.setItem('metatrace_access_token', 't1')
    const blob = new Blob(['fake'], { type: 'image/png' })
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, blob: async () => blob } as Response)
    globalThis.fetch = fetchMock as unknown as typeof fetch
    const createMock = vi.fn(() => 'blob:fake-url')
    globalThis.URL.createObjectURL = createMock as unknown as typeof URL.createObjectURL
    globalThis.URL.revokeObjectURL = vi.fn()

    const { container } = render(<AuthenticatedImage src="/api/thumb/1" alt="t" />)
    await waitFor(() => expect(fetchMock).toHaveBeenCalled())
    expect(fetchMock).toHaveBeenCalledWith('/api/thumb/1', expect.objectContaining({ headers: { Authorization: 'Bearer t1' } }))
    await waitFor(() => expect(container.querySelector('img')).not.toBeNull())
  })

  it('uses plain src without auth when no token', async () => {
    globalThis.fetch = vi.fn() as unknown as typeof fetch
    const { container } = render(<AuthenticatedImage src="/api/thumb/1" alt="t" />)
    await waitFor(() => expect(container.querySelector('img')).not.toBeNull())
    expect(globalThis.fetch).not.toHaveBeenCalled()
    expect(container.querySelector('img')?.getAttribute('src')).toBe('/api/thumb/1')
  })
})
