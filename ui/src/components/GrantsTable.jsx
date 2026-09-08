import { Anchor, Badge, Table, Text } from '@mantine/core'
import { Link } from 'react-router-dom'

import { accessColor } from '../lib/phase'
import { NoRows } from './NoRows'

/**
 * A user's access. The role column is the group whose *membership* the login
 * role holds - it never holds object privileges itself, which is why changing
 * a level is two statements and no object-level churn.
 */
export function GrantsTable({ grants }) {
  if (!grants?.length) {
    return (
      <NoRows
        title="No grants"
        description="The user declares no access, or has not been reconciled yet."
      />
    )
  }

  return (
    <Table.ScrollContainer minWidth={520} type="native">
      <Table verticalSpacing="xs" fz="sm">
        <Table.Thead>
          <Table.Tr>
            <Table.Th>Database</Table.Th>
            <Table.Th w={110}>Access</Table.Th>
            <Table.Th>Group role</Table.Th>
            <Table.Th>PostgresDB</Table.Th>
          </Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          {grants.map((grant) => (
            <Table.Tr key={`${grant.db_ref}/${grant.database}`}>
              <Table.Td>
                <Text ff="monospace" fz="sm">
                  {grant.database}
                </Text>
              </Table.Td>
              <Table.Td>
                <Badge color={accessColor(grant.role)} size="sm">
                  {grant.role}
                </Badge>
              </Table.Td>
              <Table.Td>
                <Text ff="monospace" fz="sm">
                  {grant.group_role}
                </Text>
              </Table.Td>
              <Table.Td>
                {grant.db_ref ? (
                  <Anchor component={Link} to={`/databases/${grant.db_ref}`} fz="sm">
                    {grant.db_ref}
                  </Anchor>
                ) : (
                  <Text c="dimmed" fz="sm">
                    —
                  </Text>
                )}
              </Table.Td>
            </Table.Tr>
          ))}
        </Table.Tbody>
      </Table>
    </Table.ScrollContainer>
  )
}
