import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import BrowseView from '../BrowseView'

vi.mock('../../api', () => ({
  browseImages: vi.fn().mockResolvedValue({ items: [], total: 0, offset: 0, limit: 60, has_more: false, next_cursor: null, cursor: null }),
  prewarmThumbnails: vi.fn().mockResolvedValue({ queued: 0, size: 512 }),
}))

import { browseImages, prewarmThumbnails } from '../../api'

describe('BrowseView', () => {
  beforeEach(() => vi.clearAllMocks())

  it('renders and calls browseImages on mount with default params', async () => {
    render(<BrowseView />)
    await waitFor(() => expect(browseImages).toHaveBeenCalled())
    const call = (browseImages as any).mock.calls[0][0]
    expect(call).toMatchObject({ offset: 0, limit: 60, sort: 'mtime', order: 'desc' })
  })

  it('shows placeholder when no results', async () => {
    render(<BrowseView />)
    await waitFor(() => expect(screen.getByText(/No images in the index yet/i)).toBeInTheDocument())
  })

  it('prewarms 512px thumbnails for the current browse page', async () => {
    ;(browseImages as any).mockResolvedValueOnce({
      items: [
        { id: 7, rel_path: 'a.png', original_path: 'a.png', size: 1, mtime: 1, sha256: null, width: 10, height: 10, xmp: {}, indexed_at: '2024-01-01T00:00:00Z', thumb_url: '/api/thumb/7', file_url: '/api/file/7' },
        { id: 8, rel_path: 'b.png', original_path: 'b.png', size: 1, mtime: 1, sha256: null, width: 10, height: 10, xmp: {}, indexed_at: '2024-01-01T00:00:00Z', thumb_url: '/api/thumb/8', file_url: '/api/file/8' },
      ],
      total: 2,
      offset: 0,
      limit: 60,
      has_more: false,
      next_cursor: null,
      cursor: null,
    })

    render(<BrowseView />)

    await waitFor(() => expect(prewarmThumbnails).toHaveBeenCalledWith([7, 8], 512))
  })
})
