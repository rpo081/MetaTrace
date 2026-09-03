import { describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { AddUserModal } from '../AddUserModal'

describe('AddUserModal', () => {
  async function submit() {
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /Create user/i }))
    })
  }

  it('shows PASSWORD_HINT on weak password', async () => {
    render(<AddUserModal open={true} onCancel={() => {}} onCreated={() => {}} />)
    fireEvent.change(screen.getByLabelText(/Username/i), { target: { value: 'alice' } })
    fireEvent.change(screen.getByLabelText(/Password/i), { target: { value: 'short' } })
    await submit()
    const alerts = await screen.findAllByRole('alert')
    expect(alerts.some((el) => /8\+ chars/.test(el.textContent || ''))).toBe(true)
  })

  it('validates upper/lower/digit', async () => {
    render(<AddUserModal open={true} onCancel={() => {}} onCreated={() => {}} />)
    fireEvent.change(screen.getByLabelText(/Username/i), { target: { value: 'alice' } })
    fireEvent.change(screen.getByLabelText(/Password/i), { target: { value: 'alllowercase1' } })
    await submit()
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
    render(<AddUserModal open={true} onCancel={() => {}} onCreated={onCreated} />)
    fireEvent.change(screen.getByLabelText(/Username/i), { target: { value: 'bob' } })
    fireEvent.change(screen.getByLabelText(/Password/i), { target: { value: 'Good-Pass123' } })
    await submit()
    await waitFor(() => expect(onCreated).toHaveBeenCalled())
    await waitFor(() => expect(screen.getByLabelText(/Username/i)).toHaveValue(''))
    expect(screen.getByLabelText(/Password/i)).toHaveValue('')
    expect(screen.getByRole('button', { name: /Create user/i })).toBeDisabled()
  })
})
