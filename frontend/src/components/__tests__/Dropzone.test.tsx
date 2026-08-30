import { render, screen, fireEvent } from '@testing-library/react'
import { describe, it, expect, vi } from 'vitest'
import Dropzone from '../Dropzone'

describe('Dropzone', () => {
  it('renders as button with tabIndex 0', () => {
    render(<Dropzone previewUrl={null} onFile={vi.fn()} />)
    const el = screen.getByRole('button', { name: /upload query image/i })
    expect(el).toHaveAttribute('tabIndex', '0')
    expect(el).toHaveAttribute('role', 'button')
  })

  it('disabled sets tabIndex -1', () => {
    render(<Dropzone previewUrl={null} onFile={vi.fn()} disabled />)
    const el = screen.getByRole('button', { name: /upload query image/i })
    expect(el).toHaveAttribute('tabIndex', '-1')
  })

  it('keyboard Enter/Space opens picker', () => {
    const onFile = vi.fn()
    render(<Dropzone previewUrl={null} onFile={onFile} />)
    const el = screen.getByRole('button', { name: /upload query image/i })
    const input = document.querySelector('input[type="file"]') as HTMLInputElement
    const clickSpy = vi.spyOn(input, 'click')
    fireEvent.keyDown(el, { key: 'Enter' })
    expect(clickSpy.mock.calls.length).toBeGreaterThanOrEqual(1)
    const afterEnter = clickSpy.mock.calls.length
    fireEvent.keyDown(el, { key: ' ' })
    expect(clickSpy.mock.calls.length).toBeGreaterThan(afterEnter)
    const beforeEscape = clickSpy.mock.calls.length
    fireEvent.keyDown(el, { key: 'Escape' })
    // Escape should not open picker further
    expect(clickSpy.mock.calls.length).toBe(beforeEscape)
  })

  it('shows preview when previewUrl set', () => {
    render(<Dropzone previewUrl="blob:fake" onFile={vi.fn()} onClear={vi.fn()} />)
    const img = screen.getByAltText('query') as HTMLImageElement
    expect(img.src).toContain('blob:fake')
    expect(screen.getByLabelText('Remove query image')).toBeInTheDocument()
  })
})
