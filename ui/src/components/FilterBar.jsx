import { Button, Group, Select, TextInput } from '@mantine/core'
import { IconFilterOff, IconSearch } from '@tabler/icons-react'

export function FilterBar({ fields, filters }) {
  return (
    <Group gap="sm" align="flex-end" wrap="wrap">
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
            size="xs"
            w={field.width ?? 150}
          />
        ) : (
          <TextInput
            key={field.name}
            label={field.label}
            placeholder={field.placeholder ?? 'any'}
            leftSection={<IconSearch size={14} />}
            value={filters.values[field.name]}
            onChange={(event) => filters.set(field.name, event.currentTarget.value)}
            size="xs"
            w={field.width ?? 170}
          />
        ),
      )}
      <Button
        variant="subtle"
        color="gray"
        size="xs"
        leftSection={<IconFilterOff size={14} />}
        onClick={filters.clear}
        disabled={!filters.active}
      >
        Clear
      </Button>
    </Group>
  )
}
