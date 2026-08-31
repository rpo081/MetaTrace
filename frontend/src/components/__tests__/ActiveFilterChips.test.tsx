import { render, screen, fireEvent } from '@testing-library/react'
import { describe, it, expect, vi } from 'vitest'
import ActiveFilterChips from '../ActiveFilterChips'

describe('ActiveFilterChips', () => {
  it('renders null when no filters', () => {
    const { container } = render(<ActiveFilterChips filters={{} as any} onRemove={vi.fn()} onClearAll={vi.fn()} />)
    expect(container.innerHTML).toBe('')
  })

  it('renders chips and escapes q content as text', () => {
    const filters: any = { q: '<script>alert(1)</script>', ext: 'png' }
    render(<ActiveFilterChips filters={filters} onRemove={vi.fn()} onClearAll={vi.fn()} />)
    // q is rendered as text, not HTML
    expect(screen.getByText('Search: "<script>alert(1)</script>"')).toBeInTheDocument()
    // no script element injected
    expect(document.querySelector('script')).toBeNull()
    // text is escaped in HTML source
    expect(document.body.innerHTML).toContain('&lt;script&gt;')
  })

  it('calls onRemove and onClearAll', () => {
    const onRemove = vi.fn()
    const onClearAll = vi.fn()
    const filters: any = { q: 'hello', ext: 'jpg', folder: 'a' }
    render(<ActiveFilterChips filters={filters} onRemove={onRemove} onClearAll={onClearAll} />)
    const removeBtns = screen.getAllByLabelText(/Remove filter:/)
    fireEvent.click(removeBtns[0])
    expect(onRemove).toHaveBeenCalledTimes(1)
    fireEvent.click(screen.getByText('Clear all'))
    expect(onClearAll).toHaveBeenCalledTimes(1)
  })
})
