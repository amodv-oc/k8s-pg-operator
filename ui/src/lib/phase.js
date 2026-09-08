/** The phase vocabulary, and the one place its colours are decided. */

const COLORS = {
  Ready: 'emerald',
  Drifted: 'amber',
  Pending: 'blue',
  Paused: 'slate',
  Failed: 'red',
  Unreachable: 'orange',
  Unknown: 'zinc',
}

export function phaseColor(phase) {
  return COLORS[phase] ?? 'zinc'
}

/**
 * Whether a phase warrants a look. Mirrors the API's own rule in
 * `StateStore.overview`, which treats Ready and Paused as fine and everything
 * else as worth listing.
 */
export function needsAttention(phase) {
  return phase !== 'Ready' && phase !== 'Paused'
}

/** Count tiles, trouble first. Keys match the `PhaseCounts` response model. */
export const COUNTS = [
  { key: 'failed', label: 'Failed', phase: 'Failed' },
  { key: 'unreachable', label: 'Unreachable', phase: 'Unreachable' },
  { key: 'drifted', label: 'Drifted', phase: 'Drifted' },
  { key: 'pending', label: 'Pending', phase: 'Pending' },
  { key: 'unknown', label: 'Unknown', phase: 'Unknown' },
  { key: 'paused', label: 'Paused', phase: 'Paused' },
  { key: 'ready', label: 'Ready', phase: 'Ready' },
]

/**
 * A condition's colour depends on its type, not just its status: `Drifted=True`
 * is the bad case while `Ready=True` is the good one. Drift is amber rather
 * than red because the resource did reconcile - something about the live state
 * diverges anyway.
 */
const GOOD_WHEN_TRUE = new Set(['Ready', 'Reachable', 'Synced', 'Available'])

export function conditionColor(type, status) {
  if (status === 'Unknown') return 'zinc'
  const isTrue = status === 'True'
  const good = GOOD_WHEN_TRUE.has(type) ? isTrue : !isTrue
  if (good) return 'emerald'
  return type === 'Drifted' ? 'amber' : 'red'
}

/** Access levels, in privilege order, with a stable colour each. */
export const ACCESS_COLORS = {
  OWNER: 'violet',
  RW: 'blue',
  RO: 'cyan',
}

export function accessColor(role) {
  return ACCESS_COLORS[role] ?? 'zinc'
}
