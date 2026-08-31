import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import ResultGrid from '../ResultGrid'
import type { SearchResult } from '../../types'

const mockResults: SearchResult[] = [
  { id: 1, score: 0.95, exact: false, rel_path: 'folder/a.png', original_path: '\\\\nas\\folder\\a.png', width: 100, height: 100, xmp: {}, thumb_url: '/api/thumb/1', file_url: '/api/file/1' },
  { id: 2, score: 0.8, exact: true, rel_path: 'b.jpg', original_path: '\\\\nas\\b.jpg', width: 200, height: 200, xmp: {}, thumb_url: '/api/thumb/2', file_url: '/api/file/2' },
]

describe('ResultGrid', () => {
  it('renders cards as buttons with aria-pressed', () => {
    const onSelect = vi.fn()
    render(<ResultGrid results={mockResults as any} selectedId={1} onSelect={onSelect} />)
    const buttons = screen.getAllByRole('button')
    expect(buttons.length).toBe(2)
    expect(buttons[0]).toHaveAttribute('aria-pressed', 'true')
    expect(buttons[1]).toHaveAttribute('aria-pressed', 'false')
  })

  it('has accessible aria-label with basename', () => {
    const onSelect = vi.fn()
    render(<ResultGrid results={mockResults as any} selectedId={null} onSelect={onSelect} />)
    const buttons = screen.getAllByRole('button')
    expect(buttons[0].getAttribute('aria-label')).toContain('a.png')
  })

  it('calls onSelect on click', () => {
    const onSelect = vi.fn()
    render(<ResultGrid results={mockResults as any} selectedId={null} onSelect={onSelect} />)
    fireEvent.click(screen.getAllByRole('button')[0])
    expect(onSelect).toHaveBeenCalledWith(expect.objectContaining({ id: 1 }))
  })

  it('shows exact badge when exact=true', () => {
    const onSelect = vi.fn()
    render(<ResultGrid results={mockResults as any} selectedId={null} onSelect={onSelect} />)
    expect(screen.getByText('EXACT')).toBeInTheDocument()
  })
})
