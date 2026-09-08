import { Badge, Group, Table, Text, Tooltip } from '@mantine/core'

import { schemaRows, STATE_META } from '../lib/resources'
import { NoRows } from './NoRows'

/**
 * What the operator holds on each schema, read back from the server.
 *
 * This is the audit surface of the whole access model: a user never holds
 * object privileges directly, so `status.schemaGrants` is the only place the
 * effective grants are visible.
 */
export function SchemaTable({ database }) {
  const rows = schemaRows(database)
  if (!rows.length) {
    return <NoRows title="No schemas reported" description="The database has not been reconciled yet." />
  }

  return (
    <Table.ScrollContainer minWidth={640} type="native">
      <Table verticalSpacing="xs" fz="sm">
        <Table.Thead>
          <Table.Tr>
            <Table.Th>Schema</Table.Th>
            <Table.Th w={130}>State</Table.Th>
            <Table.Th>Grants held by the group roles</Table.Th>
          </Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          {rows.map((row) => {
            const meta = STATE_META[row.state]
            return (
              <Table.Tr key={row.name}>
                <Table.Td>
                  <Text ff="monospace" fz="sm">
                    {row.name}
                  </Text>
                </Table.Td>
                <Table.Td>
                  <Group gap={6} wrap="nowrap">
                    <Tooltip label={meta.hint} multiline maw={320}>
                      <Badge color={meta.color} size="sm">
                        {meta.label}
                      </Badge>
                    </Tooltip>
                    {row.unowned && row.state !== 'unowned' && (
                      <Tooltip label={STATE_META.unowned.hint} multiline maw={320}>
                        <Badge color={STATE_META.unowned.color} size="sm" variant="outline">
                          unowned
                        </Badge>
                      </Tooltip>
                    )}
                  </Group>
                </Table.Td>
                <Table.Td>
                  {row.grants.length ? (
                    <Group gap={6}>
                      {row.grants.map((grant) => (
                        <Badge key={grant} color="slate" size="sm" variant="outline" ff="monospace">
                          {grant}
                        </Badge>
                      ))}
                    </Group>
                  ) : (
                    <Text c="dimmed" fz="xs">
                      none read back
                    </Text>
                  )}
                </Table.Td>
              </Table.Tr>
            )
          })}
        </Table.Tbody>
      </Table>
    </Table.ScrollContainer>
  )
}
