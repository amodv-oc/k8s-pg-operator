const UNITS = ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB']

/** `12 KiB`, `1.5 GiB` - the same rendering the operator writes into status. */
export function formatBytes(value) {
  if (value === null || value === undefined) return null
  let amount = Number(value)
  for (const unit of UNITS) {
    if (Math.abs(amount) < 1024 || unit === UNITS.at(-1)) {
      if (unit === 'B') return `${Math.trunc(amount)} B`
      return `${amount.toFixed(1).replace(/\.0$/, '')} ${unit}`
    }
    amount /= 1024
  }
  return null
}

/**
 * A threshold as its author wrote it (`512Mi`), or as bytes when it was given
 * as a bare number - `1073741824` reads better as `1 GiB`.
 */
export function formatThreshold(written, bytes) {
  return /^\d+$/.test(String(written)) ? formatBytes(bytes) : String(written)
}
