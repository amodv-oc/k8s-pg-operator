import { Badge, Tooltip } from '@mantine/core'

import { phaseColor } from '../lib/phase'

export function PhaseBadge({ phase, message, size = 'sm' }) {
  const badge = (
    <Badge color={phaseColor(phase)} size={size} variant="light">
      {phase || 'Unknown'}
    </Badge>
  )
  // The message is the condition that explains the phase, which for anything
  // other than Ready is the only thing worth reading.
  return message ? <Tooltip label={message} multiline maw={420}>{badge}</Tooltip> : badge
}
