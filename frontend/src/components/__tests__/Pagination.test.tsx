import { render, screen } from '@testing-library/react'
import { describe, it, expect, vi } from 'vitest'
import Pagination from '../Pagination'

describe('Pagination', () => {
  it('returns null when total 0', () => {
    const { container } = render(<Pagination offset={0} limit={60} total={0} hasMore={false} onPrev={vi.fn()} onNext={vi.fn()} />)
    expect(container.innerHTML).toBe('')
  })

  it('shows range and disables Prev at offset 0', () => {
    render(<Pagination offset={0} limit={10} total={25} hasMore={true} onPrev={vi.fn()} onNext={vi.fn()} />)
    expect(screen.getByText('Showing 1–10 of 25')).toBeInTheDocument()
    expect(screen.getByText('Prev')).toBeDisabled()
    expect(screen.getByText('Next')).toBeEnabled()
  })

  it('disables Next when hasMore false', () => {
    render(<Pagination offset={20} limit={10} total={25} hasMore={false} onPrev={vi.fn()} onNext={vi.fn()} />)
    expect(screen.getByText('Next')).toBeDisabled()
    expect(screen.getByText('Prev')).toBeEnabled()
  })
})
