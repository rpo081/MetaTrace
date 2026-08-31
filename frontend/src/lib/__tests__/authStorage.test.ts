import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { authHeaders, clearAllTokens, getAccessToken, getAdminToken, setAccessToken, authenticatedUrl } from '../authStorage'

beforeEach(() => clearAllTokens())
afterEach(() => clearAllTokens())

describe('authStorage', () => {
  it('getAccessToken returns null when empty', () => {
    expect(getAccessToken()).toBeNull()
    expect(getAdminToken()).toBeNull()
  })
  it('setAccessToken writes and clears', () => {
    setAccessToken('tok123')
    expect(getAccessToken()).toBe('tok123')
    setAccessToken(null)
    expect(getAccessToken()).toBeNull()
  })
  it('authHeaders prefers Bearer', () => {
    localStorage.setItem('metatrace_access_token', 'bear')
    localStorage.setItem('metatrace_admin_token', 'legacy')
    expect(authHeaders()).toEqual({ Authorization: 'Bearer bear' })
  })
  it('authHeaders falls back to X-Admin-Token', () => {
    localStorage.setItem('metatrace_admin_token', 'legacy')
    expect(authHeaders()).toEqual({ 'X-Admin-Token': 'legacy' })
  })
  it('authHeaders empty when no token', () => {
    expect(authHeaders()).toEqual({})
  })
  it('authenticatedUrl appends token', () => {
    localStorage.setItem('metatrace_access_token', 'a b&c')
    const url = authenticatedUrl('/api/thumb/1?size=256')
    expect(url).toBe('/api/thumb/1?size=256&token=a%20b%26c')
  })
  it('authenticatedUrl without token returns plain', () => {
    expect(authenticatedUrl('/api/thumb/1')).toBe('/api/thumb/1')
  })
})
