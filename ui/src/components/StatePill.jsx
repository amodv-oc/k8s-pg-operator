import { REPLICATION_STATES, stateTone } from '../lib/phase'
import { Pill } from './Pill'

/** A replication's state - Streaming, Lagging, Stalled... - with what it means on hover. */
export function StatePill({ state, message, ...props }) {
  if (!state) return null
  return (
    <Pill
      tone={stateTone(state)}
      label={state}
      message={message || REPLICATION_STATES[state]?.hint}
      {...props}
    />
  )
}
