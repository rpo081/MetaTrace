import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import ImageViewerModal from '../ImageViewerModal'
import type { SearchResult } from '../../types'

vi.mock('../../features/auth/api', () => ({
  getSignedFileUrl: vi.fn(),
}))

import { getSignedFileUrl } from '../../features/auth/api'

const mockImages = [
  { id: 1, score: 0.95, exact: false, rel_path: 'folder/a.png', original_path: '\\\\nas\\folder\\a.png', width: 800, height: 600, xmp: {}, thumb_url: '/api/thumb/1', file_url: '/api/file/1' },
  { id: 2, score: 0.8, exact: false, rel_path: 'b.jpg', original_path: '\\\\nas\\b.jpg', width: 200, height: 100, xmp: {}, thumb_url: '/api/thumb/2', file_url: '/api/file/2' },
] as unknown as SearchResult[]

function mockBlobFetch() {
  globalThis.fetch = vi.fn().mockResolvedValue({
    ok: true,
    blob: async () => new Blob(['fake-image'], { type: 'image/png' }),
  }) as unknown as typeof fetch
}

describe('ImageViewerModal', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    globalThis.URL.createObjectURL = vi.fn(() => 'blob:mock-url') as unknown as typeof URL.createObjectURL
    globalThis.URL.revokeObjectURL = vi.fn() as unknown as typeof URL.revokeObjectURL
    mockBlobFetch()
    ;(getSignedFileUrl as unknown as ReturnType<typeof vi.fn>).mockImplementation(
      async (id: number) => `/api/file/${id}?token=signed`,
    )
  })

  async function renderReady(index = 0, props: Partial<React.ComponentProps<typeof ImageViewerModal>> = {}) {
    const onClose = vi.fn()
    const onNavigate = vi.fn()
    render(
      <ImageViewerModal
        open
        images={mockImages}
        index={index}
        onClose={onClose}
        onNavigate={onNavigate}
        {...props}
      />,
    )
    const img = (await screen.findByAltText('a.png', {}, { timeout: 2000 })) as HTMLImageElement
    fireEvent.load(img)
    return { onClose, onNavigate, img }
  }

  it('renders dialog with filename, dims and counter', async () => {
    const onClose = vi.fn()
    const onNavigate = vi.fn()
    render(<ImageViewerModal open images={mockImages} index={0} onClose={onClose} onNavigate={onNavigate} />)
    expect(await screen.findByRole('dialog')).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: /a\.png/ })).toBeInTheDocument()
    expect(screen.getByText('1 von 2')).toBeInTheDocument()
    expect(screen.getByText(/800×600/)).toBeInTheDocument()
    // Signed URL is used for the original (no Authorization header in markup).
    await waitFor(() => expect(getSignedFileUrl).toHaveBeenCalledWith(1))
    await waitFor(() => expect(globalThis.fetch).toHaveBeenCalledWith(
      '/api/file/1?token=signed',
      expect.anything(),
    ))
  })

  it('disables prev at start and navigates via next', async () => {
    const { onNavigate } = await renderReady(0)
    expect(screen.getByRole('button', { name: 'Vorheriges Bild' })).toBeDisabled()
    const next = screen.getByRole('button', { name: 'Nächstes Bild' })
    expect(next).not.toBeDisabled()
    fireEvent.click(next)
    expect(onNavigate).toHaveBeenCalledWith(1)
  })

  it('disables next at the end', async () => {
    const onClose = vi.fn()
    const onNavigate = vi.fn()
    render(<ImageViewerModal open images={mockImages} index={1} onClose={onClose} onNavigate={onNavigate} />)
    await screen.findByRole('dialog')
    expect(screen.getByRole('button', { name: 'Nächstes Bild' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Vorheriges Bild' })).not.toBeDisabled()
    expect(screen.getByText('2 von 2')).toBeInTheDocument()
  })

  it('zooms in/out via toolbar buttons with live zoom label', async () => {
    await renderReady(0)
    const label = screen.getByLabelText('Zoomstufe')
    expect(label).toHaveTextContent('100 %')
    fireEvent.click(screen.getByRole('button', { name: 'Vergrößern' }))
    expect(label).toHaveTextContent('125 %')
    fireEvent.click(screen.getByRole('button', { name: 'Verkleinern' }))
    expect(label).toHaveTextContent('100 %')
  })

  it('double-click toggles fit <-> 200%', async () => {
    const { img } = await renderReady(0)
    const label = screen.getByLabelText('Zoomstufe')
    fireEvent.doubleClick(img)
    expect(label).toHaveTextContent('200 %')
    fireEvent.doubleClick(img)
    expect(label).toHaveTextContent('100 %')
  })

  it('keyboard: +/- zoom, arrows navigate, escape closes', async () => {
    const { onClose, onNavigate } = await renderReady(0)
    const label = screen.getByLabelText('Zoomstufe')
    fireEvent.keyDown(document, { key: '+' })
    expect(label).toHaveTextContent('125 %')
    fireEvent.keyDown(document, { key: '-' })
    expect(label).toHaveTextContent('100 %')
    fireEvent.keyDown(document, { key: 'ArrowRight' })
    expect(onNavigate).toHaveBeenCalledWith(1)
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(onClose).toHaveBeenCalled()
  })

  it('shows error box with retry when the original cannot load', async () => {
    vi.mocked(globalThis.fetch).mockResolvedValueOnce({ ok: false } as unknown as Response)
    const onClose = vi.fn()
    const onNavigate = vi.fn()
    render(<ImageViewerModal open images={mockImages} index={0} onClose={onClose} onNavigate={onNavigate} />)
    expect(await screen.findByRole('alert')).toBeInTheDocument()
    const callsBefore = (getSignedFileUrl as unknown as ReturnType<typeof vi.fn>).mock.calls.length
    fireEvent.click(screen.getByRole('button', { name: 'Erneut versuchen' }))
    await waitFor(() =>
      expect((getSignedFileUrl as unknown as ReturnType<typeof vi.fn>).mock.calls.length).toBeGreaterThan(callsBefore),
    )
  })

  it('returns null when closed', () => {
    const { container } = render(
      <ImageViewerModal open={false} images={mockImages} index={0} onClose={vi.fn()} onNavigate={vi.fn()} />,
    )
    expect(container).toBeEmptyDOMElement()
  })
})
