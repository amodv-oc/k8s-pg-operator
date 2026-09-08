import { Skeleton, Stack } from '@mantine/core'

export function TableSkeleton({ rows = 4 }) {
  return (
    <Stack gap="xs">
      {Array.from({ length: rows }, (_, index) => (
        <Skeleton key={index} height={28} radius="sm" />
      ))}
    </Stack>
  )
}
