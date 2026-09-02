import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiError } from '../../../api'
import { AuthProvider, type AuthApi } from '../AuthContext'
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
  // Wrap with AuthProvider so RequireAuth / Context types resolve — the
  // mock above short-circuits useAuth regardless.
  return render(
    <AuthProvider>
      <LoginPage />
    </AuthProvider>,
  )
}

describe('LoginPage', () => {
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

    const user = userEvent.setup()
    await user.type(screen.getByLabelText(/username/i), 'alice')
    await user.type(screen.getByLabelText(/password/i), 'bad')
    await user.click(screen.getByRole('button', { name: /sign in/i }))

    expect(await screen.findByText(/invalid username or password/i)).toBeInTheDocument()
  })

  it('submits_credentials_on_click', async () => {
    authApiMock.login.mockResolvedValueOnce(undefined)
    renderLogin()

    const user = userEvent.setup()
    await user.type(screen.getByLabelText(/username/i), 'alice')
    await user.type(screen.getByLabelText(/password/i), 'Good-Password-123')
    await user.click(screen.getByRole('button', { name: /sign in/i }))

    expect(authApiMock.login).toHaveBeenCalledWith('alice', 'Good-Password-123')
  })

  it('lockout_message_for_423', async () => {
    authApiMock.login.mockRejectedValueOnce(new ApiError(423, 'locked'))
    renderLogin()
    const user = userEvent.setup()
    await user.type(screen.getByLabelText(/username/i), 'a')
    await user.type(screen.getByLabelText(/password/i), 'b')
    await user.click(screen.getByRole('button', { name: /sign in/i }))
    expect(await screen.findByText(/account temporarily locked/i)).toBeInTheDocument()
  })
})