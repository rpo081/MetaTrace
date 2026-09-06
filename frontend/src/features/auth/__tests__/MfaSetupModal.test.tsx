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
    const enrollBodies: unknown[] = []
    const confirmBodies: unknown[] = []
    fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input.toString()
      if (url.endsWith('/api/auth/mfa/status')) {
        return jsonResponse({ enabled: false, enrolled_at: null, backup_remaining: 0, has_pending: false })
      }
      if (url.endsWith('/api/auth/mfa/enroll')) {
        enrollBodies.push(init?.body ? JSON.parse(String(init.body)) : null)
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
        confirmBodies.push(init?.body ? JSON.parse(String(init.body)) : null)
        return jsonResponse({ ok: true, backup_codes: codes })
      }
      throw new Error(`unexpected fetch ${url} ${init?.method}`)
    })

    render(<MfaSetupModal onCancel={() => {}} />)
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /enable 2fa/i })).toBeInTheDocument()
    })

    // Enabling rotates the pending secret — the password is required.
    fireEvent.change(screen.getByLabelText(/current password/i), { target: { value: 'Good-Password-123' } })
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /enable 2fa/i }))
    })
    expect(enrollBodies).toEqual([{ password: 'Good-Password-123' }])

    // QR + manual key + code field appear. The password carries over from
    // the overview step into the confirm form (re-auth binds the factor).
    expect(await screen.findByAltText(/qr code for authenticator enrollment/i)).toBeInTheDocument()
    expect(screen.getByText('JBSWY3DPEHPK3PXP')).toBeInTheDocument()

    fireEvent.change(screen.getByLabelText(/authenticator code/i), { target: { value: '123456' } })
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /^confirm$/i }))
    })
    expect(confirmBodies).toEqual([{ code: '123456', password: 'Good-Password-123' }])

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

  it('pending_enrollment_offers_continue_without_reenrolling', async () => {
    // has_pending → "Continue enrollment" re-fetches the existing QR only
    // (no POST /enroll, so the pending secret is not rotated).
    const seen: string[] = []
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input.toString()
      seen.push(url)
      if (url.endsWith('/api/auth/mfa/status')) {
        return jsonResponse({ enabled: false, enrolled_at: null, backup_remaining: 0, has_pending: true })
      }
      if (url.endsWith('/api/auth/mfa/qr')) {
        return {
          ok: true,
          status: 200,
          blob: async () => new Blob(['fake-png'], { type: 'image/png' }),
        } as unknown as Response
      }
      throw new Error(`unexpected fetch ${url}`)
    })

    render(<MfaSetupModal onCancel={() => {}} />)
    const continueBtn = await screen.findByRole('button', { name: /continue enrollment/i })
    // Both Continue and Start over are offered for a pending enrollment.
    expect(screen.getByRole('button', { name: /start over/i })).toBeInTheDocument()
    await act(async () => {
      fireEvent.click(continueBtn)
    })

    expect(await screen.findByAltText(/qr code for authenticator enrollment/i)).toBeInTheDocument()
    expect(seen.some((u) => u.endsWith('/api/auth/mfa/enroll'))).toBe(false)
    // No manual-entry key on resume (only POST /enroll returns the secret).
    expect(screen.queryByText(/manual entry key/i)).toBeNull()
  })

  it('continue_enrollment_falls_back_to_fresh_enroll_on_404_only', async () => {
    // Pending state cleared server-side (QR 404) → resume falls back to a
    // fresh POST /enroll (with the entered password) so the user can still
    // self-activate.
    const seen: string[] = []
    fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input.toString()
      seen.push(url)
      if (url.endsWith('/api/auth/mfa/status')) {
        return jsonResponse({ enabled: false, enrolled_at: null, backup_remaining: 0, has_pending: true })
      }
      if (url.endsWith('/api/auth/mfa/qr')) {
        return jsonResponse({ detail: 'no pending MFA enrollment' }, { status: 404, ok: false })
      }
      if (url.endsWith('/api/auth/mfa/enroll')) {
        expect(init?.body).toContain('Good-Password-123')
        return jsonResponse({ otpauth_url: 'otpauth://totp/x', secret: 'JBSWY3DPEHPK3PXP' })
      }
      throw new Error(`unexpected fetch ${url}`)
    })

    render(<MfaSetupModal onCancel={() => {}} />)
    const continueBtn = await screen.findByRole('button', { name: /continue enrollment/i })
    fireEvent.change(screen.getByLabelText(/current password/i), { target: { value: 'Good-Password-123' } })
    await act(async () => {
      fireEvent.click(continueBtn)
    })

    // Fresh enrollment: manual-entry key from POST /enroll is shown.
    expect(await screen.findByText('JBSWY3DPEHPK3PXP')).toBeInTheDocument()
    expect(seen.some((u) => u.endsWith('/api/auth/mfa/enroll'))).toBe(true)
  })

  it('continue_enrollment_does_not_rotate_on_qr_server_error', async () => {
    // A transient QR failure (500) must NOT silently rotate the pending
    // secret — the error is surfaced instead.
    const seen: string[] = []
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input.toString()
      seen.push(url)
      if (url.endsWith('/api/auth/mfa/status')) {
        return jsonResponse({ enabled: false, enrolled_at: null, backup_remaining: 0, has_pending: true })
      }
      if (url.endsWith('/api/auth/mfa/qr')) {
        return jsonResponse({ detail: 'internal error' }, { status: 500, ok: false })
      }
      throw new Error(`unexpected fetch ${url}`)
    })

    render(<MfaSetupModal onCancel={() => {}} />)
    const continueBtn = await screen.findByRole('button', { name: /continue enrollment/i })
    await act(async () => {
      fireEvent.click(continueBtn)
    })

    await waitFor(() => {
      expect(screen.getByRole('alert')).toBeInTheDocument()
    })
    expect(seen.some((u) => u.endsWith('/api/auth/mfa/enroll'))).toBe(false)
    expect(screen.queryByAltText(/qr code for authenticator enrollment/i)).toBeNull()
  })

  it('expired_pending_shows_start_over_warning', async () => {
    // pending_expired (410-cleared or stale) → expiry warning + Start over,
    // no Continue button (nothing left to continue).
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input.toString()
      if (url.endsWith('/api/auth/mfa/status')) {
        return jsonResponse({ enabled: false, enrolled_at: null, backup_remaining: 0, has_pending: false, pending_expired: true })
      }
      throw new Error(`unexpected fetch ${url}`)
    })

    render(<MfaSetupModal onCancel={() => {}} />)
    const warning = await screen.findByRole('status')
    expect(warning).toHaveTextContent(/enrollment expired/i)
    expect(screen.getByRole('button', { name: /start over/i })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /continue enrollment/i })).toBeNull()
  })

  it('continue_enrollment_handles_expired_410_without_rotating', async () => {
    // QR 410 (pending expired between status poll and resume) → expiry
    // error, no silent rotation via POST /enroll.
    const seen: string[] = []
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input.toString()
      seen.push(url)
      if (url.endsWith('/api/auth/mfa/status')) {
        return jsonResponse({ enabled: false, enrolled_at: null, backup_remaining: 0, has_pending: true })
      }
      if (url.endsWith('/api/auth/mfa/qr')) {
        return jsonResponse({ detail: 'enrollment expired' }, { status: 410, ok: false })
      }
      throw new Error(`unexpected fetch ${url}`)
    })

    render(<MfaSetupModal onCancel={() => {}} />)
    const continueBtn = await screen.findByRole('button', { name: /continue enrollment/i })
    await act(async () => {
      fireEvent.click(continueBtn)
    })

    await waitFor(() => {
      expect(screen.getByRole('alert')).toHaveTextContent(/enrollment expired/i)
    })
    expect(seen.some((u) => u.endsWith('/api/auth/mfa/enroll'))).toBe(false)
    expect(screen.queryByAltText(/qr code for authenticator enrollment/i)).toBeNull()
  })

  it('low_backup_codes_show_regeneration_warning', async () => {
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input.toString()
      if (url.endsWith('/api/auth/mfa/status')) {
        return jsonResponse({ enabled: true, enrolled_at: '2026-01-01', backup_remaining: 1, has_pending: false })
      }
      throw new Error(`unexpected fetch ${url}`)
    })

    render(<MfaSetupModal onCancel={() => {}} />)
    const warning = await screen.findByRole('status')
    expect(warning).toHaveTextContent(/only 1 unused backup code remains/i)
    expect(warning).toHaveTextContent(/generate a new set soon/i)
  })

  it('healthy_backup_count_shows_no_warning', async () => {
    mockStatus(true, 7)
    render(<MfaSetupModal onCancel={() => {}} />)
    await waitFor(() => {
      expect(screen.getByText(/7 unused backup codes/i)).toBeInTheDocument()
    })
    expect(screen.queryByText(/generate a new set soon/i)).toBeNull()
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
