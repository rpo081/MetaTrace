import { describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { AddUserModal } from '../AddUserModal'

describe('AddUserModal', () => {
  it('shows PASSWORD_HINT on weak password', async () => {
    const user = userEvent.setup()
    render(<AddUserModal open={true} onCancel={() => {}} onCreated={() => {}} />)
    await user.type(screen.getByLabelText(/Username/i), 'alice')
    await user.type(screen.getByLabelText(/Password/i), 'short')
    await user.click(screen.getByRole('button', { name: /Create user/i }))
    const alerts = await screen.findAllByRole('alert')
    expect(alerts.some((el) => /8\+ chars/.test(el.textContent || ''))).toBe(true)
  })

  it('validates upper/lower/digit', async () => {
    const user = userEvent.setup()
    render(<AddUserModal open={true} onCancel={() => {}} onCreated={() => {}} />)
    await user.type(screen.getByLabelText(/Username/i), 'alice')
    await user.type(screen.getByLabelText(/Password/i), 'alllowercase1')
    await user.click(screen.getByRole('button', { name: /Create user/i }))
    const alerts = await screen.findAllByRole('alert')
    expect(alerts.some((el) => /8\+ chars/.test(el.textContent || ''))).toBe(true)
  })

  it('focuses username input on open, not Cancel', async () => {
    render(<AddUserModal open={true} onCancel={() => {}} onCreated={() => {}} />)
    await waitFor(() => expect(screen.getByLabelText(/Username/i)).toHaveFocus())
    expect(screen.getByRole('button', { name: /Cancel/i })).not.toHaveFocus()
  })

  it('calls onCreated on valid submit', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ id: 1, username: 'bob', email: 'bob@metatrace.local', role: 'viewer', is_active: true, created_at: '', last_login: null }) } as unknown as Response)
    const onCreated = vi.fn()
    const user = userEvent.setup()
    render(<AddUserModal open={true} onCancel={() => {}} onCreated={onCreated} />)
    await user.type(screen.getByLabelText(/Username/i), 'bob')
    await user.type(screen.getByLabelText(/Password/i), 'Good-Pass123')
    await user.click(screen.getByRole('button', { name: /Create user/i }))
    await waitFor(() => expect(onCreated).toHaveBeenCalled())
  })
})
