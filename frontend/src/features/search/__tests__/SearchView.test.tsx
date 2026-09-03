import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, waitFor } from '@testing-library/react'
import SearchView from '../SearchView'

vi.mock('../../../api', async () => {
  const actual = await vi.importActual('../../../api')
  return {
    ...actual,
    prewarmThumbnails: vi.fn().mockResolvedValue({ queued: 0, size: 512 }),
  }
})

vi.mock('../useSearch', () => ({
  useSearch: vi.fn(),
}))

import { prewarmThumbnails } from '../../../api'
import { useSearch } from '../useSearch'

describe('SearchView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    ;(useSearch as any).mockReturnValue({
      file: null,
      previewUrl: null,
      textQuery: '',
      combineMode: 'and',
      k: 5,
      minScore: 0,
      loading: false,
      error: null,
      response: {
        total_indexed: 2,
        exact_match: false,
        results: [
          { id: 4, score: 0.9, exact: false, rel_path: 'a.png', original_path: 'a.png', width: 10, height: 10, xmp: {}, thumb_url: '/api/thumb/4', file_url: '/api/file/4' },
          { id: 5, score: 0.8, exact: false, rel_path: 'b.png', original_path: 'b.png', width: 10, height: 10, xmp: {}, thumb_url: '/api/thumb/5', file_url: '/api/file/5' },
        ],
      },
      selected: null,
      onFile: vi.fn(),
      onClearFile: vi.fn(),
      setTextQuery: vi.fn(),
      setCombineMode: vi.fn(),
      setK: vi.fn(),
      setMinScore: vi.fn(),
      runSearch: vi.fn(),
      setSelected: vi.fn(),
    })
  })

  it('prewarms 512px thumbnails for current search results', async () => {
    render(<SearchView stats={null} refreshStats={vi.fn()} notice={null} error={null} />)

    await waitFor(() => expect(prewarmThumbnails).toHaveBeenCalledWith([4, 5], 512))
  })
})