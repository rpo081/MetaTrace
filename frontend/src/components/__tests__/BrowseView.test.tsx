import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import BrowseView from '../BrowseView'

vi.mock('../../api', () => ({
  browseImages: vi.fn().mockResolvedValue({ items: [], total: 0, offset: 0, limit: 60, has_more: false, next_cursor: null, cursor: null })
}))

import { browseImages } from '../../api'

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
})
