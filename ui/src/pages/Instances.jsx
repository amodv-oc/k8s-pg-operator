import { Anchor, Code, Table, Text } from '@mantine/core'
import { Link } from 'react-router-dom'

import { ErrorState } from '../components/ErrorState'
import { FilterBar } from '../components/FilterBar'
import { NoRows } from '../components/NoRows'
import { Page } from '../components/Page'
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
  const rows = data ?? []

  return (
    <Page>
      <PageHeader
        title="Instances"
        subtitle="Cluster-scoped PostgresInstance resources. Deleting one never touches the server."
      />

      <Panel
        bleed={rows.length > 0}
        actions={
          <FilterBar
            filters={filters}
            fields={[{ name: 'phase', label: 'Phase', type: 'select', options: PHASE_OPTIONS }]}
            summary={data && `${rows.length} ${rows.length === 1 ? 'instance' : 'instances'}`}
          />
        }
      >
        {isError && <ErrorState error={error} />}
        {isPending && !isError && <TableSkeleton />}
        {data && !rows.length && (
          <NoRows
            title="No instances"
            description={
              filters.active
                ? 'No instance matches these filters.'
                : 'Register a server with a PostgresInstance to get started.'
            }
          />
        )}
        {rows.length > 0 && (
          <Table.ScrollContainer minWidth={980} type="native">
            <Table fz="sm">
              <Table.Thead>
                <Table.Tr>
                  <Table.Th>Name</Table.Th>
                  <Table.Th w={120}>Phase</Table.Th>
                  <Table.Th>Endpoint</Table.Th>
                  <Table.Th w={84}>Version</Table.Th>
                  <Table.Th>Managing role</Table.Th>
                  <Table.Th w={100}>SSL</Table.Th>
                  <Table.Th w={70}>DBs</Table.Th>
                  <Table.Th w={74}>Users</Table.Th>
                  <Table.Th w={150}>Connected</Table.Th>
                </Table.Tr>
              </Table.Thead>
              <Table.Tbody>
                {rows.map((instance) => (
                  <Table.Tr key={instance.name}>
                    <Table.Td>
                      <Anchor component={Link} to={`/instances/${instance.name}`} fw={600}>
                        {instance.name}
                      </Anchor>
                    </Table.Td>
                    <Table.Td>
                      <PhaseBadge phase={instance.phase} message={instance.message} />
                    </Table.Td>
                    <Table.Td>
                      <Text fz="xs" ff="monospace" c="dimmed" className="pgop-wrap">
                        {instance.endpoint ?? '—'}
                      </Text>
                    </Table.Td>
                    <Table.Td className="pgop-numeric">{instance.server_version ?? '—'}</Table.Td>
                    <Table.Td>
                      <Text fz="xs" ff="monospace" c="dimmed">
                        {instance.managing_role ?? '—'}
                      </Text>
                    </Table.Td>
                    <Table.Td>
                      <Code fz="xs">{instance.ssl_mode ?? '—'}</Code>
                    </Table.Td>
                    <Table.Td className="pgop-numeric">{instance.databases}</Table.Td>
                    <Table.Td className="pgop-numeric">{instance.users}</Table.Td>
                    <Table.Td>
                      <RelativeTime value={instance.last_connected_at} fz="sm" c="dimmed" />
                    </Table.Td>
                  </Table.Tr>
                ))}
              </Table.Tbody>
            </Table>
          </Table.ScrollContainer>
        )}
      </Panel>
    </Page>
  )
}
