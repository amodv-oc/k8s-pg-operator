import { keepPreviousData, useQuery } from '@tanstack/react-query'

import { apiGet, fetchHealth } from './api'
import { useRefetchInterval } from './refresh'

// The API caches every response for `api.cacheTtl` seconds (5 by default), so
// a poll inside that window costs one round trip and no API-server call at
// all. staleTime matches the TTL; the poll interval is twice it.
const STALE_MS = 5000

// Keeping the previous page rendered while the next one loads is what makes a
// polled table readable: without it every refetch blanks the rows.
const LIST_OPTIONS = { staleTime: STALE_MS, placeholderData: keepPreviousData }

export function useHealth() {
  const refetchInterval = useRefetchInterval()
  return useQuery({
    queryKey: ['health'],
    queryFn: fetchHealth,
    refetchInterval,
    staleTime: STALE_MS,
  })
}

export function useOverview() {
  const refetchInterval = useRefetchInterval()
  return useQuery({
    queryKey: ['overview'],
    queryFn: () => apiGet('/api/v1/overview'),
    refetchInterval,
    ...LIST_OPTIONS,
  })
}

export function useInstances(params) {
  const refetchInterval = useRefetchInterval()
  return useQuery({
    queryKey: ['instances', params ?? {}],
    queryFn: () => apiGet('/api/v1/instances', params),
    refetchInterval,
    ...LIST_OPTIONS,
  })
}

export function useInstance(name) {
  const refetchInterval = useRefetchInterval()
  return useQuery({
    queryKey: ['instance', name],
    queryFn: () => apiGet(`/api/v1/instances/${encodeURIComponent(name)}`),
    enabled: Boolean(name),
    refetchInterval,
    staleTime: STALE_MS,
  })
}

export function useDatabases(params) {
  const refetchInterval = useRefetchInterval()
  return useQuery({
    queryKey: ['databases', params ?? {}],
    queryFn: () => apiGet('/api/v1/databases', params),
    refetchInterval,
    ...LIST_OPTIONS,
  })
}

export function useDatabase(namespace, name) {
  const refetchInterval = useRefetchInterval()
  return useQuery({
    queryKey: ['database', namespace, name],
    queryFn: () =>
      apiGet(`/api/v1/databases/${encodeURIComponent(namespace)}/${encodeURIComponent(name)}`),
    enabled: Boolean(namespace && name),
    refetchInterval,
    staleTime: STALE_MS,
  })
}

export function useUsers(params) {
  const refetchInterval = useRefetchInterval()
  return useQuery({
    queryKey: ['users', params ?? {}],
    queryFn: () => apiGet('/api/v1/users', params),
    refetchInterval,
    ...LIST_OPTIONS,
  })
}

export function useUser(namespace, name) {
  const refetchInterval = useRefetchInterval()
  return useQuery({
    queryKey: ['user', namespace, name],
    queryFn: () =>
      apiGet(`/api/v1/users/${encodeURIComponent(namespace)}/${encodeURIComponent(name)}`),
    enabled: Boolean(namespace && name),
    refetchInterval,
    staleTime: STALE_MS,
  })
}
