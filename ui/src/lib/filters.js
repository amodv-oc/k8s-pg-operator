import { useCallback } from 'react'
import { useSearchParams } from 'react-router-dom'

/**
 * List filters, held in the URL rather than in component state.
 *
 * The names map one-to-one onto the API's own query parameters, so a filtered
 * view is both a single request and a shareable link.
 */
export function useFilters(names) {
  const [searchParams, setSearchParams] = useSearchParams()

  // Rebuilt each render rather than memoised: the object feeds react-query
  // keys, which are compared structurally, so a fresh identity costs nothing.
  const values = {}
  for (const name of names) {
    values[name] = searchParams.get(name) ?? ''
  }

  const set = useCallback(
    (name, value) => {
      setSearchParams(
        (previous) => {
          const next = new URLSearchParams(previous)
          if (value) next.set(name, value)
          else next.delete(name)
          return next
        },
        { replace: true },
      )
    },
    [setSearchParams],
  )

  const clear = useCallback(() => {
    setSearchParams(new URLSearchParams(), { replace: true })
  }, [setSearchParams])

  return { values, set, clear, active: Object.values(values).some(Boolean) }
}
