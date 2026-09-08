/**
 * Runtime configuration, fetched once before the app renders.
 *
 * The chart mounts `config.json` from a ConfigMap so one image can be labelled
 * per cluster without a rebuild. It is optional: the defaults below are the
 * correct values for the in-pod deployment, where nginx proxies the API on
 * loopback and the UI is therefore same-origin.
 */

const DEFAULTS = {
  /** Prefix for API requests. Empty means same-origin, which is the norm. */
  apiBaseUrl: '',
  /** Shown in the header so two tabs on two clusters are distinguishable. */
  clusterLabel: '',
  /** Poll interval. The API caches for 5s, so anything above that is cheap. */
  refreshIntervalMs: 10000,
}

let current = { ...DEFAULTS }

export function config() {
  return current
}

export async function loadConfig() {
  try {
    const response = await fetch('config.json', { cache: 'no-store' })
    if (response.ok) {
      const body = await response.json()
      current = {
        ...DEFAULTS,
        ...body,
        refreshIntervalMs: Math.max(1000, Number(body.refreshIntervalMs) || DEFAULTS.refreshIntervalMs),
      }
    }
  } catch {
    // No ConfigMap, or a malformed one. The defaults are usable, and failing
    // to start over an optional label would be worse than ignoring it.
  }
  return current
}
