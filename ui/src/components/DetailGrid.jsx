import { DataList, Text } from '@mantine/core'

const isEmpty = (value) =>
  value === undefined || value === null || value === '' || (Array.isArray(value) && !value.length)

/**
 * Label/value rows for the detail pages.
 *
 * Entries with no value are dropped rather than shown as blanks: the three
 * resources populate different subsets of their status depending on how far
 * reconciliation got, and empty rows would bury the fields that are set.
 */
export function DetailGrid({ items, labelWidth = 165, ...props }) {
  const present = items.filter((item) => !isEmpty(item.value))
  if (!present.length) {
    return (
      <Text c="dimmed" fz="sm">
        Nothing reported yet.
      </Text>
    )
  }
  return (
    <DataList withDivider gap="xs" labelWidth={labelWidth} {...props}>
      {present.map((item) => (
        <DataList.Item key={item.label}>
          <DataList.ItemLabel>{item.label}</DataList.ItemLabel>
          <DataList.ItemValue className="pgop-wrap">{item.value}</DataList.ItemValue>
        </DataList.Item>
      ))}
    </DataList>
  )
}
