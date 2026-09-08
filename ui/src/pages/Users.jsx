import { Anchor, Badge, Group, Stack, Table, Text } from '@mantine/core'
import { Link } from 'react-router-dom'

import { ErrorState } from '../components/ErrorState'
import { FilterBar } from '../components/FilterBar'
import { NoRows } from '../components/NoRows'
import { PageHeader } from '../components/PageHeader'
import { Panel } from '../components/Panel'
import { PhaseBadge } from '../components/PhaseBadge'
import { RelativeTime } from '../components/RelativeTime'
import { TableSkeleton } from '../components/TableSkeleton'
import { useFilters } from '../lib/filters'
import { accessColor, COUNTS } from '../lib/phase'
import { useUsers } from '../lib/queries'

const FIELDS = ['namespace', 'instance', 'database', 'access', 'phase']
const PHASE_OPTIONS = COUNTS.map(({ phase }) => phase)

export function Users() {
  const filters = useFilters(FIELDS)
  const { data, isPending, isError, error } = useUsers(filters.values)

  return (
    <>
      <PageHeader
        title="Users"
        subtitle="A PostgresUser is a LOGIN role granted membership of a database's group role — it holds no object privileges of its own."
      />

      <Panel
        actions={
          <FilterBar
            filters={filters}
            fields={[
              { name: 'namespace', label: 'Namespace' },
              { name: 'instance', label: 'Instance' },
              { name: 'database', label: 'Database' },
              {
                name: 'access',
                label: 'Access',
                type: 'select',
                width: 120,
                options: ['OWNER', 'RW', 'RO'],
              },
              { name: 'phase', label: 'Phase', type: 'select', options: PHASE_OPTIONS },
            ]}
          />
        }
      >
        <Stack gap="md">
          {isError && <ErrorState error={error} />}
          {isPending && !isError && <TableSkeleton />}
          {data && !data.length && (
            <NoRows
              title="No users"
              description={
                filters.active
                  ? 'No user matches these filters.'
                  : 'Create a PostgresUser declaring the access it needs.'
              }
            />
          )}
          {data && data.length > 0 && (
            <Table.ScrollContainer minWidth={980} type="native">
              <Table fz="sm">
                <Table.Thead>
                  <Table.Tr>
                    <Table.Th>Name</Table.Th>
                    <Table.Th>Namespace</Table.Th>
                    <Table.Th w={110}>Phase</Table.Th>
                    <Table.Th>Role</Table.Th>
                    <Table.Th>Instance</Table.Th>
                    <Table.Th>Access</Table.Th>
                    <Table.Th w={100}>Retention</Table.Th>
                    <Table.Th w={130}>Reconciled</Table.Th>
                  </Table.Tr>
                </Table.Thead>
                <Table.Tbody>
                  {data.map((user) => (
                    <Table.Tr key={`${user.namespace}/${user.name}`}>
                      <Table.Td>
                        <Anchor
                          component={Link}
                          to={`/users/${user.namespace}/${user.name}`}
                          fw={500}
                        >
                          {user.name}
                        </Anchor>
                      </Table.Td>
                      <Table.Td>
                        <Text fz="xs" c="dimmed">
                          {user.namespace}
                        </Text>
                      </Table.Td>
                      <Table.Td>
                        <PhaseBadge phase={user.phase} message={user.message} />
                      </Table.Td>
                      <Table.Td>
                        <Text fz="xs" ff="monospace">
                          {user.username ?? '—'}
                        </Text>
                      </Table.Td>
                      <Table.Td>
                        {user.instance ? (
                          <Anchor component={Link} to={`/instances/${user.instance}`} fz="sm">
                            {user.instance}
                          </Anchor>
                        ) : (
                          '—'
                        )}
                      </Table.Td>
                      <Table.Td>
                        <Group gap={4}>
                          {user.grants.length ? (
                            user.grants.map((grant) => (
                              <Badge
                                key={`${grant.database}/${grant.role}`}
                                color={accessColor(grant.role)}
                                size="sm"
                              >
                                {grant.role} · {grant.database}
                              </Badge>
                            ))
                          ) : (
                            <Text fz="xs" c="dimmed">
                              none
                            </Text>
                          )}
                        </Group>
                      </Table.Td>
                      <Table.Td>
                        <Badge
                          color={user.retention_policy === 'DROP' ? 'red' : 'slate'}
                          variant="outline"
                          size="sm"
                        >
                          {user.retention_policy ?? '—'}
                        </Badge>
                      </Table.Td>
                      <Table.Td>
                        <RelativeTime value={user.last_reconciled_at} fz="xs" c="dimmed" />
                      </Table.Td>
                    </Table.Tr>
                  ))}
                </Table.Tbody>
              </Table>
            </Table.ScrollContainer>
          )}
        </Stack>
      </Panel>
    </>
  )
}
