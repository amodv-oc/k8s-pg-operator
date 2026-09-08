import { useMemo, useState } from 'react'

import { config } from '../lib/config'
import { RefreshContext } from '../lib/refresh'

export function RefreshProvider({ children }) {
  const [live, setLive] = useState(true)
  const intervalMs = config().refreshIntervalMs
  const value = useMemo(() => ({ live, setLive, intervalMs }), [live, intervalMs])

  return <RefreshContext.Provider value={value}>{children}</RefreshContext.Provider>
}
