import { Badge, Group, Text, Tooltip } from '@mantine/core'

import { extensionRows, STATE_META } from '../lib/resources'

/**
 * Declared and installed extensions. `plpgsql` shows up as merely `observed`
 * on every database because PostgreSQL installs it into template1 - it is not
 * managed here, and is never touched.
 */
export function ExtensionList({ database }) {
  const rows = extensionRows(database)
  if (!rows.length) {
    return (
      <Text c="dimmed" fz="sm">
        No extensions declared or observed.
      </Text>
    )
  }
  return (
    <Group gap="xs">
      {rows.map((row) => {
        const meta = STATE_META[row.state]
        return (
          <Tooltip key={row.name} label={meta.hint} multiline maw={320}>
            <Badge color={meta.color} variant={row.state === 'managed' ? 'light' : 'outline'}>
              {row.name}
              {row.version && ` ${row.version}`}
              {row.state !== 'managed' && ` · ${meta.label}`}
            </Badge>
          </Tooltip>
        )
      })}
    </Group>
  )
}
