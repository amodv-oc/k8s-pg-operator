/**
 * The phase vocabulary, and the one place its colours are decided.
 *
 * Colour is expressed as a *tone* rather than a palette shade. A tone names a
 * background and foreground pair defined in `theme/global.css` for both colour
 * schemes, which is what lets one phase read identically as a table cell, a
 * summary chip, a condition row and a page heading — and lets the dark scheme
 * restate the pair as translucency over green instead of re-deriving it.
 */

const PHASE_TONES = {
  Ready: 'Ready',
  Drifted: 'Drifted',
  Pending: 'Pending',
  Paused: 'Paused',
  Failed: 'Failed',
  Unreachable: 'Unreachable',
  Unknown: 'Unknown',
}

export function phaseTone(phase) {
  return PHASE_TONES[phase] ?? 'Unknown'
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
 * A condition's tone depends on its type, not just its status: `Drifted=True`
 * is the bad case while `Ready=True` is the good one. Drift takes the gold
 * rather than the red because the resource did reconcile - something about the
 * live state diverges anyway.
 */
const GOOD_WHEN_TRUE = new Set(['Ready', 'Reachable', 'Synced', 'Available'])

export function conditionTone(type, status) {
  if (status === 'Unknown') return 'Unknown'
  const isTrue = status === 'True'
  const good = GOOD_WHEN_TRUE.has(type) ? isTrue : !isTrue
  if (good) return 'Ready'
  return type === 'Drifted' ? 'Drifted' : 'Failed'
}

/** Access levels, in privilege order, with a stable tone each. */
export const ACCESS_TONES = {
  OWNER: 'OWNER',
  RW: 'RW',
  RO: 'RO',
}

export function accessTone(role) {
  return ACCESS_TONES[role] ?? 'Unknown'
}

/**
 * Retention. RETAIN is the quiet default and reads as neutral chrome; DROP is
 * the one that deletes a real database or role, so it is never quiet.
 */
export function retentionTone(policy) {
  return policy === 'DROP' ? 'Failed' : 'Paused'
}
