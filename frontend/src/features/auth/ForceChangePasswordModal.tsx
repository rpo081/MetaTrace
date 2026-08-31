import { useAuth } from './AuthContext'
import { ChangePasswordModal } from './ChangePasswordModal'

export function ForceChangePasswordModal() {
  const { state } = useAuth()
  if (state.status !== 'authenticated' || !state.mustChangePassword) return null
  return <ChangePasswordModal mode="self" dismissible={false} />
}