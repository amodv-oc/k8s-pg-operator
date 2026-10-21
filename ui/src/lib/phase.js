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
  Degraded: 'Degraded',
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
  { key: 'degraded', label: 'Degraded', phase: 'Degraded' },
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
const GOOD_WHEN_TRUE = new Set(['Ready', 'Reachable', 'Synced', 'Available', 'Streaming'])

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

/**
 * A replication's state, from its Streaming condition. Lagging, Stalled and
 * SlotLost are what make the phase Degraded; Initializing and Disabled are
 * healthy - a copy in progress, and a subscription switched off on purpose.
 */
export const REPLICATION_STATES = {
  Streaming: { tone: 'Ready', hint: 'changes are flowing within the alerting thresholds' },
  Initializing: { tone: 'Pending', hint: 'some tables are still in their initial copy' },
  Disabled: { tone: 'Paused', hint: 'spec.enabled is false; the slot keeps retaining WAL' },
  Lagging: { tone: 'Degraded', hint: 'lag or retained WAL is over its alerting threshold' },
  Stalled: { tone: 'Failed', hint: 'changes are not being applied' },
  SlotLost: { tone: 'Failed', hint: 'the slot is gone; the replica must be rebuilt' },
}

export function stateTone(state) {
  return REPLICATION_STATES[state]?.tone ?? 'Unknown'
}
