import { EmptyState } from '@mantine/core'
import { IconDatabaseOff } from '@tabler/icons-react'

export function NoRows({ title, description, icon }) {
  return (
    <EmptyState
      size="sm"
      my="lg"
      icon={icon ?? <IconDatabaseOff size={22} />}
      title={title}
      description={description}
    />
  )
}
