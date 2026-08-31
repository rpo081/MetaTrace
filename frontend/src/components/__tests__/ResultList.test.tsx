import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import ResultList from '../ResultList'
import type { BrowseImage } from '../../types'

const mockResults: BrowseImage[] = [
  { id: 1, rel_path: 'folder/a.png', original_path: '\\\\nas\\folder\\a.png', width: 100, height: 100, xmp: {}, size: 1000, mtime: 1700000000, sha256: null, indexed_at: '2024-01-01T00:00:00Z', thumb_url: '/api/thumb/1', file_url: '/api/file/1' },
  { id: 2, rel_path: 'b.jpg', original_path: '\\\\nas\\b.jpg', width: 200, height: 200, xmp: {}, size: 2000, mtime: 1700000000, sha256: null, indexed_at: '2024-01-01T00:00:00Z', thumb_url: '/api/thumb/2', file_url: '/api/file/2' },
]

describe('ResultList', () => {
  it('renders listitems with button role', () => {
    const onSelect = vi.fn()
    render(<ResultList results={mockResults as any} selectedId={null} onSelect={onSelect} />)
    const list = screen.getByRole('list')
    expect(list).toBeInTheDocument()
    const buttons = screen.getAllByRole('button')
    expect(buttons.length).toBe(4)
  })

  it('aria-pressed reflects selection', () => {
    const onSelect = vi.fn()
    render(<ResultList results={mockResults as any} selectedId={2} onSelect={onSelect} />)
    const rows = screen.getAllByRole('button', { name: /a\.png|Open a\.png/ })
    expect(rows[0]).toHaveAttribute('aria-pressed', 'false')
    const second = screen.getAllByRole('button', { name: /b\.jpg|Open b\.jpg/ })
    expect(second[0]).toHaveAttribute('aria-pressed', 'true')
  })

  it('calls onSelect on click and has aria-label', () => {
    const onSelect = vi.fn()
    render(<ResultList results={mockResults as any} selectedId={null} onSelect={onSelect} />)
    const btn = screen.getAllByRole('button', { name: /a\.png|Open a\.png/ })[0]
    expect(btn.getAttribute('aria-label')).toContain('a.png')
    fireEvent.click(btn)
    expect(onSelect).toHaveBeenCalled()
  })
})
