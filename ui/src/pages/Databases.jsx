import { Anchor, Group, Table, Text } from '@mantine/core'
import { Link } from 'react-router-dom'

import { ErrorState } from '../components/ErrorState'
import { FilterBar } from '../components/FilterBar'
import { NoRows } from '../components/NoRows'
import { Page } from '../components/Page'
import { PageHeader } from '../components/PageHeader'
import { Panel } from '../components/Panel'
import { PhaseBadge } from '../components/PhaseBadge'
import { Pill } from '../components/Pill'
import { RelativeTime } from '../components/RelativeTime'
import { TableSkeleton } from '../components/TableSkeleton'
import { useFilters } from '../lib/filters'
import { COUNTS, retentionTone } from '../lib/phase'
import { useDatabases } from '../lib/queries'

const FIELDS = ['namespace', 'instance', 'phase', 'drifted']
const PHASE_OPTIONS = COUNTS.map(({ phase }) => phase)

export function Databases() {
  const filters = useFilters(FIELDS)
  const { data, isPending, isError, error } = useDatabases(filters.values)
  const rows = data ?? []

  return (
    <Page>
      <PageHeader
        title="Databases"
        subtitle="Each PostgresDB owns a database, its schemas and extensions, and the three group roles behind OWNER, RW and RO."
      />

      <Panel
        bleed={rows.length > 0}
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
                width: 130,
                options: [
                  { value: 'true', label: 'retained' },
                  { value: 'false', label: 'none' },
                ],
              },
            ]}
            summary={data && `${rows.length} ${rows.length === 1 ? 'database' : 'databases'}`}
          />
        }
      >
        {isError && <ErrorState error={error} />}
        {isPending && !isError && <TableSkeleton />}
        {data && !rows.length && (
          <NoRows
            title="No databases"
            description={
              filters.active
                ? 'No database matches these filters.'
                : 'Create a PostgresDB referencing a Ready instance.'
            }
          />
        )}
        {rows.length > 0 && (
          <Table.ScrollContainer minWidth={980} type="native">
            <Table fz="sm">
              <Table.Thead>
                <Table.Tr>
                  <Table.Th>Name</Table.Th>
                  <Table.Th>Namespace</Table.Th>
                  <Table.Th w={120}>Phase</Table.Th>
                  <Table.Th>Instance</Table.Th>
                  <Table.Th>Database</Table.Th>
                  <Table.Th w={110}>Retention</Table.Th>
                  <Table.Th w={84}>Schemas</Table.Th>
                  <Table.Th>Retained</Table.Th>
                  <Table.Th w={150}>Reconciled</Table.Th>
                </Table.Tr>
              </Table.Thead>
              <Table.Tbody>
                {rows.map((db) => {
                  const orphans = [...db.orphaned_schemas, ...db.orphaned_extensions]
                  return (
                    <Table.Tr key={`${db.namespace}/${db.name}`}>
                      <Table.Td>
                        <Anchor
                          component={Link}
                          to={`/databases/${db.namespace}/${db.name}`}
                          fw={600}
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
                        <Text fz="xs" ff="monospace" c="dimmed">
                          {db.database_name ?? '—'}
                        </Text>
                      </Table.Td>
                      <Table.Td>
                        <Pill
                          tone={retentionTone(db.retention_policy)}
                          variant="outline"
                          label={db.retention_policy ?? '—'}
                        />
                      </Table.Td>
                      <Table.Td className="pgop-numeric">{db.managed_schemas.length}</Table.Td>
                      <Table.Td>
                        {orphans.length ? (
                          <Group gap={4}>
                            {orphans.map((name) => (
                              <Pill key={name} tone="Drifted" label={name} ff="monospace" />
                            ))}
                          </Group>
                        ) : (
                          <Text fz="xs" c="dimmed">
                            —
                          </Text>
                        )}
                      </Table.Td>
                      <Table.Td>
                        <RelativeTime value={db.last_reconciled_at} fz="sm" c="dimmed" />
                      </Table.Td>
                    </Table.Tr>
                  )
                })}
              </Table.Tbody>
            </Table>
          </Table.ScrollContainer>
        )}
      </Panel>
    </Page>
  )
}
