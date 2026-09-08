import { createContext, useContext } from 'react'

/**
 * Polling state, shared so the header's toggle reaches every query at once.
 *
 * Kept apart from the provider component: react-refresh requires a module to
 * export either components or plain values, not both.
 */
export const RefreshContext = createContext(null)

export function useRefresh() {
  const value = useContext(RefreshContext)
  if (value === null) {
    throw new Error('useRefresh must be used within a RefreshProvider')
  }
  return value
}

/** What react-query wants for `refetchInterval`; `false` pauses polling. */
export function useRefetchInterval() {
  const { live, intervalMs } = useRefresh()
  return live ? intervalMs : false
}
