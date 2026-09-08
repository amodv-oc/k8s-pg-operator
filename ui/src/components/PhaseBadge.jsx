import { phaseTone } from '../lib/phase'
import { Pill } from './Pill'

export function PhaseBadge({ phase, message, ...props }) {
  return (
    <Pill tone={phaseTone(phase)} label={phase || 'Unknown'} message={message} {...props} />
  )
}
