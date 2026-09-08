import { Text, Tooltip } from '@mantine/core'

import { absoluteTime, relativeTime } from '../lib/time'

export function RelativeTime({ value, ...props }) {
  const relative = relativeTime(value)
  if (!relative) {
    return (
      <Text span c="dimmed" {...props}>
        —
      </Text>
    )
  }
  return (
    <Tooltip label={absoluteTime(value)}>
      {/* One phrase, so it never breaks across lines; a column too narrow for
          it widens the table into its own horizontal scroll instead. */}
      <Text span style={{ whiteSpace: 'nowrap' }} {...props}>
        {relative}
      </Text>
    </Tooltip>
  )
}
