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
      <Text span {...props}>
        {relative}
      </Text>
    </Tooltip>
  )
}
