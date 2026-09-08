import '@mantine/core/styles.css'
import './theme/global.css'

import { MantineProvider } from '@mantine/core'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'

import { App } from './App'
import { RefreshProvider } from './components/RefreshProvider'
import { loadConfig } from './lib/config'
import { theme } from './theme'

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // A 4xx is the server's final answer - a missing resource or a bad
      // filter - so retrying it only delays the message. A 5xx or a network
      // failure is worth two attempts.
      retry: (failures, error) => {
        const status = error?.status ?? 0
        if (status >= 400 && status < 500) return false
        return failures < 2
      },
      retryDelay: (attempt) => Math.min(1000 * 2 ** attempt, 5000),
      refetchOnWindowFocus: true,
    },
  },
})

// Config first: the header's cluster label and the poll interval come from a
// ConfigMap, and reading them after the first render would restart every query.
loadConfig().then(() => {
  createRoot(document.getElementById('root')).render(
    <StrictMode>
      <MantineProvider theme={theme} defaultColorScheme="auto">
        <QueryClientProvider client={queryClient}>
          <RefreshProvider>
            <BrowserRouter>
              <App />
            </BrowserRouter>
          </RefreshProvider>
        </QueryClientProvider>
      </MantineProvider>
    </StrictMode>,
  )
})
