import { Button, Group, Select, Text, TextInput } from '@mantine/core'
import { IconFilterOff, IconSearch } from '@tabler/icons-react'

/**
 * A list's filter strip.
 *
 * `summary` is the row count, pushed to the far end: a filtered table has to
 * say how much it is hiding, or a narrow filter reads as an empty cluster.
 */
export function FilterBar({ fields, filters, summary }) {
  return (
    <Group gap="sm" align="flex-end" wrap="wrap" style={{ flex: 1 }}>
      {fields.map((field) =>
        field.type === 'select' ? (
          <Select
            key={field.name}
            label={field.label}
            placeholder={field.placeholder ?? 'any'}
            data={field.options}
            value={filters.values[field.name] || null}
            onChange={(value) => filters.set(field.name, value ?? '')}
            clearable
            size="sm"
            w={field.width ?? 170}
          />
        ) : (
          <TextInput
            key={field.name}
            label={field.label}
            placeholder={field.placeholder ?? 'any'}
            leftSection={<IconSearch size={14} />}
            value={filters.values[field.name]}
            onChange={(event) => filters.set(field.name, event.currentTarget.value)}
            size="sm"
            w={field.width ?? 170}
          />
        ),
      )}
      <Button
        variant="outline"
        color="gray"
        size="sm"
        leftSection={<IconFilterOff size={14} />}
        onClick={filters.clear}
        disabled={!filters.active}
        c={filters.active ? 'var(--pgop-accent)' : undefined}
        style={filters.active ? { borderColor: 'var(--pgop-accent)' } : undefined}
      >
        Clear
      </Button>
      {summary && (
        <Text fz="sm" c="dimmed" className="pgop-numeric" ml="auto" pb={7}>
          {summary}
        </Text>
      )}
    </Group>
  )
}
