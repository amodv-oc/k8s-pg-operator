import { config } from './config'

/** A failed API call, carrying FastAPI's `detail` string for display. */
export class ApiError extends Error {
  constructor(status, detail, path) {
    super(detail || `HTTP ${status}`)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
    this.path = path
  }
}

function queryString(params) {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params ?? {})) {
    if (value === undefined || value === null || value === '') continue
    search.set(key, String(value))
  }
  const rendered = search.toString()
  return rendered ? `?${rendered}` : ''
}

async function detailOf(response) {
  // Every error path in this API answers with {"detail": "..."} - FastAPI's
  // HTTPException, the 422 validation handler and the operator's own
  // OperatorError handler alike - so that is the one field worth surfacing.
  try {
    const body = await response.json()
    if (typeof body?.detail === 'string') return body.detail
    return JSON.stringify(body)
  } catch {
    return response.statusText || `HTTP ${response.status}`
  }
}

export async function apiGet(path, params) {
  const url = `${config().apiBaseUrl}${path}${queryString(params)}`
  let response
  try {
    response = await fetch(url, { headers: { Accept: 'application/json' } })
  } catch (cause) {
    // nginx served this bundle, so the static half is fine; this means the API
    // container itself is not answering on the loopback proxy.
    throw new ApiError(0, `cannot reach the API: ${cause.message}`, path)
  }
  if (!response.ok) {
    throw new ApiError(response.status, await detailOf(response), path)
  }
  return response.json()
}

/**
 * `/readyz` answers 503 *with a body* when the cluster is unreachable or a CRD
 * is missing, and that body is the diagnosis. Treating it as an error would
 * throw away the only useful thing on the page.
 */
export async function fetchHealth() {
  const url = `${config().apiBaseUrl}/readyz`
  try {
    const response = await fetch(url, { headers: { Accept: 'application/json' } })
    const body = await response.json()
    return { ...body, httpStatus: response.status }
  } catch (cause) {
    return {
      status: 'degraded',
      kubernetes: false,
      crds: {},
      detail: `cannot reach the API: ${cause.message}`,
      httpStatus: 0,
    }
  }
}
