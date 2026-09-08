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
import { COUNTS } from '../lib/phase'
import { useDatabases } from '../lib/queries'

const FIELDS = ['namespace', 'instance', 'phase', 'drifted']
const PHASE_OPTIONS = COUNTS.map(({ phase }) => phase)

export function Databases() {
  const filters = useFilters(FIELDS)
  const { data, isPending, isError, error } = useDatabases(filters.values)

  return (
    <>
      <PageHeader
        title="Databases"
        subtitle="Each PostgresDB owns a database, its schemas and extensions, and the three group roles behind OWNER, RW and RO."
      />

      <Panel
        actions={
          <FilterBar
            filters={filters}
            fields={[
              { name: 'namespace', label: 'Namespace' },
              { name: 'instance', label: 'Instance' },
              { name: 'phase', label: 'Phase', type: 'select', options: PHASE_OPTIONS },
              {
                name: 'drifted',
                label: 'Orphans',
                type: 'select',
                width: 120,
                options: [
                  { value: 'true', label: 'retained' },
                  { value: 'false', label: 'none' },
                ],
              },
            ]}
          />
        }
      >
        <Stack gap="md">
          {isError && <ErrorState error={error} />}
          {isPending && !isError && <TableSkeleton />}
          {data && !data.length && (
            <NoRows
              title="No databases"
              description={
                filters.active
                  ? 'No database matches these filters.'
                  : 'Create a PostgresDB referencing a Ready instance.'
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
                    <Table.Th>Instance</Table.Th>
                    <Table.Th>Database</Table.Th>
                    <Table.Th w={100}>Retention</Table.Th>
                    <Table.Th w={80}>Schemas</Table.Th>
                    <Table.Th>Retained</Table.Th>
                    <Table.Th w={130}>Reconciled</Table.Th>
                  </Table.Tr>
                </Table.Thead>
                <Table.Tbody>
                  {data.map((db) => {
                    const orphans = [...db.orphaned_schemas, ...db.orphaned_extensions]
                    return (
                      <Table.Tr key={`${db.namespace}/${db.name}`}>
                        <Table.Td>
                          <Anchor
                            component={Link}
                            to={`/databases/${db.namespace}/${db.name}`}
                            fw={500}
                          >
                            {db.name}
                          </Anchor>
                        </Table.Td>
                        <Table.Td>
                          <Text fz="xs" c="dimmed">
                            {db.namespace}
                          </Text>
                        </Table.Td>
                        <Table.Td>
                          <PhaseBadge phase={db.phase} message={db.message} />
                        </Table.Td>
                        <Table.Td>
                          {db.instance ? (
                            <Anchor component={Link} to={`/instances/${db.instance}`} fz="sm">
                              {db.instance}
                            </Anchor>
                          ) : (
                            '—'
                          )}
                        </Table.Td>
                        <Table.Td>
                          <Text fz="xs" ff="monospace">
                            {db.database_name ?? '—'}
                          </Text>
                        </Table.Td>
                        <Table.Td>
                          <Badge
                            color={db.retention_policy === 'DROP' ? 'red' : 'slate'}
                            variant="outline"
                            size="sm"
                          >
                            {db.retention_policy ?? '—'}
                          </Badge>
                        </Table.Td>
                        <Table.Td className="pgop-numeric">{db.managed_schemas.length}</Table.Td>
                        <Table.Td>
                          {orphans.length ? (
                            <Group gap={4}>
                              {orphans.map((name) => (
                                <Badge key={name} color="amber" size="sm">
                                  {name}
                                </Badge>
                              ))}
                            </Group>
                          ) : (
                            <Text fz="xs" c="dimmed">
                              —
                            </Text>
                          )}
                        </Table.Td>
                        <Table.Td>
                          <RelativeTime value={db.last_reconciled_at} fz="xs" c="dimmed" />
                        </Table.Td>
                      </Table.Tr>
                    )
                  })}
                </Table.Tbody>
              </Table>
            </Table.ScrollContainer>
          )}
        </Stack>
      </Panel>
    </>
  )
}
