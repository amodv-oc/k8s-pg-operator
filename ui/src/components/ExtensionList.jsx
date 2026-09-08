import { Group, Text } from '@mantine/core'

import { extensionRows, STATE_META } from '../lib/resources'
import { Pill } from './Pill'

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
          <Pill
            key={row.name}
            tone={meta.tone}
            message={meta.hint}
            variant={row.state === 'managed' ? 'light' : 'outline'}
            label={`${row.name}${row.version ? ` ${row.version}` : ''}${
              row.state === 'managed' ? '' : ` · ${meta.label}`
            }`}
          />
        )
      })}
    </Group>
  )
}
