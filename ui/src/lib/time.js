const RELATIVE = new Intl.RelativeTimeFormat(undefined, { numeric: 'auto' })

const UNITS = [
  ['year', 31_536_000],
  ['month', 2_592_000],
  ['week', 604_800],
  ['day', 86_400],
  ['hour', 3_600],
  ['minute', 60],
  ['second', 1],
]

/** "3 minutes ago", or null when the timestamp is absent or unparseable. */
export function relativeTime(iso, now = Date.now()) {
  if (!iso) return null
  const then = Date.parse(iso)
  if (Number.isNaN(then)) return null

  const delta = (then - now) / 1000
  for (const [unit, seconds] of UNITS) {
    if (Math.abs(delta) >= seconds || unit === 'second') {
      return RELATIVE.format(Math.round(delta / seconds), unit)
    }
  }
  return null
}

/** The full timestamp, for a tooltip on the relative one. */
export function absoluteTime(iso) {
  if (!iso) return null
  const then = Date.parse(iso)
  if (Number.isNaN(then)) return iso
  return new Date(then).toLocaleString(undefined, { timeZoneName: 'short' })
}
