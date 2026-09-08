import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// In the cluster the API is a sibling container in the same pod and nginx
// proxies these prefixes to it on 127.0.0.1. In development the same prefixes
// are proxied to a port-forwarded API, so the app is same-origin in both cases
// and never needs CORS.
const target = process.env.PGOP_API_URL || 'http://localhost:8000'
const prefixes = ['/api', '/readyz', '/healthz', '/docs', '/redoc', '/openapi.json']

export default defineConfig({
  plugins: [react()],
  // Absolute, not './'. A relative base would make a deep link such as
  // /databases/team-a/orders resolve ./assets/index.js against that path and
  // 404 after the SPA fallback served index.html.
  base: '/',
  server: {
    port: 5173,
    strictPort: true,
    proxy: Object.fromEntries(
      prefixes.map((prefix) => [prefix, { target, changeOrigin: true }]),
    ),
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
    // Mantine plus the icon set lands a little over the default 500kB warning.
    chunkSizeWarningLimit: 1000,
  },
})
