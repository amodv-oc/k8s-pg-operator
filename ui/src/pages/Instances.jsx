import { Anchor, Code, Stack, Table, Text } from '@mantine/core'
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
import { useInstances } from '../lib/queries'

const FIELDS = ['phase']
const PHASE_OPTIONS = COUNTS.map(({ phase }) => phase)

export function Instances() {
  const filters = useFilters(FIELDS)
  const { data, isPending, isError, error } = useInstances(filters.values)

  return (
    <>
      <PageHeader
        title="Instances"
        subtitle="Cluster-scoped PostgresInstance resources. Deleting one never touches the server."
      />

      <Panel
        actions={
          <FilterBar
            filters={filters}
            fields={[{ name: 'phase', label: 'Phase', type: 'select', options: PHASE_OPTIONS }]}
          />
        }
      >
        <Stack gap="md">
          {isError && <ErrorState error={error} />}
          {isPending && !isError && <TableSkeleton />}
          {data && !data.length && (
            <NoRows
              title="No instances"
              description={
                filters.active
                  ? 'No instance matches these filters.'
                  : 'Register a server with a PostgresInstance to get started.'
              }
            />
          )}
          {data && data.length > 0 && (
            <Table.ScrollContainer minWidth={980} type="native">
              <Table fz="sm">
                <Table.Thead>
                  <Table.Tr>
                    <Table.Th>Name</Table.Th>
                    <Table.Th w={110}>Phase</Table.Th>
                    <Table.Th>Endpoint</Table.Th>
                    <Table.Th w={80}>Version</Table.Th>
                    <Table.Th>Managing role</Table.Th>
                    <Table.Th w={90}>SSL</Table.Th>
                    <Table.Th w={70}>DBs</Table.Th>
                    <Table.Th w={70}>Users</Table.Th>
                    <Table.Th w={130}>Connected</Table.Th>
                  </Table.Tr>
                </Table.Thead>
                <Table.Tbody>
                  {data.map((instance) => (
                    <Table.Tr key={instance.name}>
                      <Table.Td>
                        <Anchor component={Link} to={`/instances/${instance.name}`} fw={500}>
                          {instance.name}
                        </Anchor>
                      </Table.Td>
                      <Table.Td>
                        <PhaseBadge phase={instance.phase} message={instance.message} />
                      </Table.Td>
                      <Table.Td>
                        <Text fz="xs" ff="monospace" className="pgop-wrap">
                          {instance.endpoint ?? '—'}
                        </Text>
                      </Table.Td>
                      <Table.Td className="pgop-numeric">{instance.server_version ?? '—'}</Table.Td>
                      <Table.Td>
                        <Text fz="xs" ff="monospace">
                          {instance.managing_role ?? '—'}
                        </Text>
                      </Table.Td>
                      <Table.Td>
                        <Code fz="xs">{instance.ssl_mode ?? '—'}</Code>
                      </Table.Td>
                      <Table.Td className="pgop-numeric">{instance.databases}</Table.Td>
                      <Table.Td className="pgop-numeric">{instance.users}</Table.Td>
                      <Table.Td>
                        <RelativeTime value={instance.last_connected_at} fz="xs" c="dimmed" />
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
