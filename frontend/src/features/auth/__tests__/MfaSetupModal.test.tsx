import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { AuthApi } from '../AuthContext'
import { MfaSetupModal } from '../MfaSetupModal'

const authMock = {
  state: {
    user: null,
    status: 'unauthenticated' as const,
    mustChangePassword: false,
    mfaEnabled: false,
  },
  loadingTooLong: false,
  login: vi.fn(),
  loginWithMfa: vi.fn(),
  logout: vi.fn(),
  refresh: vi.fn(),
  changePassword: vi.fn(),
  setMfaEnabled: vi.fn(),
  retryBoot: vi.fn(),
  withAuthRetry: vi.fn(),
} as unknown as AuthApi

vi.mock('../AuthContext', async () => {
  const actual = await vi.importActual<typeof import('../AuthContext')>('../AuthContext')
  return {
    ...actual,
    useAuth: () => authMock,
  }
})

function jsonResponse(body: unknown, init: { status?: number; ok?: boolean } = {}) {
  return {
    ok: init.ok ?? true,
    status: init.status ?? 200,
    json: async () => body,
  } as unknown as Response
}

let fetchMock: ReturnType<typeof vi.fn>

beforeEach(() => {
  fetchMock = vi.fn()
  globalThis.fetch = fetchMock as unknown as typeof fetch
  window.URL.createObjectURL = vi.fn(() => 'blob:fake-qr')
  window.URL.revokeObjectURL = vi.fn()
  vi.clearAllMocks()
})

afterEach(() => {
  vi.restoreAllMocks()
})

function mockStatus(enabled: boolean, backup = 0) {
  fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
    const url = typeof input === 'string' ? input : input.toString()
    if (url.endsWith('/api/auth/mfa/status')) {
      return jsonResponse({ enabled, enrolled_at: null, backup_remaining: backup, has_pending: false })
    }
    throw new Error(`unexpected fetch ${url}`)
  })
}

describe('MfaSetupModal', () => {
  it('shows_disabled_status_with_enable_button', async () => {
    mockStatus(false)
    render(<MfaSetupModal onCancel={() => {}} />)

    await waitFor(() => {
      expect(screen.getByText(/two-factor authentication is disabled/i)).toBeInTheDocument()
    })
    expect(screen.getByRole('button', { name: /enable 2fa/i })).toBeInTheDocument()
    // One status fetch on mount — no poll loop (regression: effect deps).
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1))
    await act(async () => {
      await new Promise((r) => setTimeout(r, 50))
    })
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('shows_enabled_status_with_backup_count', async () => {
    mockStatus(true, 7)
    render(<MfaSetupModal onCancel={() => {}} />)

    await waitFor(() => {
      expect(screen.getByText(/is enabled/i)).toBeInTheDocument()
    })
    expect(screen.getByText(/7 unused backup codes/i)).toBeInTheDocument()
  })

  it('enroll_confirm_keeps_backup_codes_visible_until_done', async () => {
    // Regression test: confirm used to trigger refresh() which flipped back
    // to 'overview', so the once-displayed codes were never shown.
    const codes = ['AAAA-1111-2222', 'BBBB-3333-4444']
    fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input.toString()
      if (url.endsWith('/api/auth/mfa/status')) {
        return jsonResponse({ enabled: false, enrolled_at: null, backup_remaining: 0, has_pending: false })
      }
      if (url.endsWith('/api/auth/mfa/enroll')) {
        return jsonResponse({ otpauth_url: 'otpauth://totp/x', secret: 'JBSWY3DPEHPK3PXP' })
      }
      if (url.endsWith('/api/auth/mfa/qr')) {
        return {
          ok: true,
          status: 200,
          blob: async () => new Blob(['fake-png'], { type: 'image/png' }),
        } as unknown as Response
      }
      if (url.endsWith('/api/auth/mfa/confirm') && init?.method === 'POST') {
        return jsonResponse({ ok: true, backup_codes: codes })
      }
      throw new Error(`unexpected fetch ${url} ${init?.method}`)
    })

    render(<MfaSetupModal onCancel={() => {}} />)
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /enable 2fa/i })).toBeInTheDocument()
    })

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /enable 2fa/i }))
    })

    // QR + manual key + code field appear.
    expect(await screen.findByAltText(/qr code for authenticator enrollment/i)).toBeInTheDocument()
    expect(screen.getByText('JBSWY3DPEHPK3PXP')).toBeInTheDocument()

    fireEvent.change(screen.getByLabelText(/authenticator code/i), { target: { value: '123456' } })
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /^confirm$/i }))
    })

    // Backup codes must be displayed and STAY displayed (no auto-refresh).
    expect(await screen.findByText('AAAA-1111-2222')).toBeInTheDocument()
    expect(screen.getByText('BBBB-3333-4444')).toBeInTheDocument()
    await act(async () => {
      await new Promise((r) => setTimeout(r, 50))
    })
    expect(screen.getByText('AAAA-1111-2222')).toBeInTheDocument()

    // Done returns to the overview.
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /^done$/i }))
    })
    await waitFor(() => {
      expect(screen.queryByText('AAAA-1111-2222')).toBeNull()
    })
  })

  it('escape_closes_modal', async () => {
    mockStatus(false)
    const onCancel = vi.fn()
    render(<MfaSetupModal onCancel={onCancel} />)
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /enable 2fa/i })).toBeInTheDocument()
    })

    await act(async () => {
      fireEvent.keyDown(document, { key: 'Escape' })
    })
    expect(onCancel).toHaveBeenCalledTimes(1)
  })
})
