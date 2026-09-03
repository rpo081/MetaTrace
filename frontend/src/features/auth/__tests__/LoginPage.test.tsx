import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiError } from '../../../api'
import { type AuthApi } from '../AuthContext'
import { LoginPage } from '../LoginPage'

// A minimal stub AuthContext for testing LoginPage in isolation — LoginPage
// only needs `login()` and the resulting state. We give it a stub
// implementation and verify the LoginPage wires up to it correctly.
const authApiMock = {
  state: { user: null, status: 'unauthenticated' as const, mustChangePassword: false },
  loadingTooLong: false,
  login: vi.fn(),
  logout: vi.fn(),
  refresh: vi.fn(),
  changePassword: vi.fn(),
  retryBoot: vi.fn(),
  withAuthRetry: vi.fn(),
} as unknown as AuthApi & { login: ReturnType<typeof vi.fn> }

vi.mock('../AuthContext', async () => {
  const actual = await vi.importActual<typeof import('../AuthContext')>('../AuthContext')
  return {
    ...actual,
    useAuth: () => authApiMock,
  }
})

beforeEach(() => {
  authApiMock.login.mockReset()
})

afterEach(() => {
  vi.restoreAllMocks()
})

function renderLogin() {
  return render(<LoginPage />)
}

describe('LoginPage', () => {
  async function submit() {
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /sign in/i }))
    })
  }

  it('renders_username_and_password_fields', () => {
    renderLogin()
    expect(document.querySelector('.login-version')).toHaveTextContent(/\S/)
    expect(screen.getByLabelText(/username/i)).toBeInTheDocument()
    expect(screen.getByLabelText(/password/i)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /sign in/i })).toBeInTheDocument()
  })

  it('autofocuses_username_field', () => {
    renderLogin()
    expect(screen.getByLabelText(/username/i)).toHaveFocus()
  })

  it('shows_401_message', async () => {
    authApiMock.login.mockRejectedValueOnce(new ApiError(401, 'invalid credentials'))
    renderLogin()

    fireEvent.change(screen.getByLabelText(/username/i), { target: { value: 'alice' } })
    fireEvent.change(screen.getByLabelText(/password/i), { target: { value: 'bad' } })
    await submit()

    expect(await screen.findByText(/invalid username or password/i)).toBeInTheDocument()
  })

  it('submits_credentials_on_click', async () => {
    authApiMock.login.mockResolvedValueOnce(undefined)
    renderLogin()

    fireEvent.change(screen.getByLabelText(/username/i), { target: { value: 'alice' } })
    fireEvent.change(screen.getByLabelText(/password/i), { target: { value: 'Good-Password-123' } })
    await submit()

    await waitFor(() => expect(authApiMock.login).toHaveBeenCalledWith('alice', 'Good-Password-123'))
    expect(screen.getByRole('button', { name: /signing in/i })).toBeDisabled()
  })

  it('lockout_message_for_423', async () => {
    authApiMock.login.mockRejectedValueOnce(new ApiError(423, 'locked'))
    renderLogin()
    fireEvent.change(screen.getByLabelText(/username/i), { target: { value: 'a' } })
    fireEvent.change(screen.getByLabelText(/password/i), { target: { value: 'b' } })
    await submit()
    expect(await screen.findByText(/account temporarily locked/i)).toBeInTheDocument()
  })
})