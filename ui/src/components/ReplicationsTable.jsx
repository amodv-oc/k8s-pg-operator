import { Anchor, Group, Table, Text } from '@mantine/core'
import { IconArrowRight } from '@tabler/icons-react'
import { Link } from 'react-router-dom'

import { PhaseBadge } from './PhaseBadge'
import { RelativeTime } from './RelativeTime'
import { StatePill } from './StatePill'

function End({ end }) {
  if (!end?.display) {
    return (
      <Text fz="xs" ff="monospace" c="dimmed">
        {end?.postgres_db ?? '—'}
      </Text>
    )
  }
  return (
    <Anchor component={Link} to={`/databases/${end.postgres_db}`} fz="xs" ff="monospace">
      {end.display}
    </Anchor>
  )
}

/**
 * Replications as rows: the list page, and the instance and database pages,
 * which show the replications running from or into them. Source and target
 * are spelled out, so direction needs no column of its own.
 */
export function ReplicationsTable({ rows, showNamespace = true }) {
  return (
    <Table.ScrollContainer minWidth={980} type="native">
      <Table fz="sm">
        <Table.Thead>
          <Table.Tr>
            <Table.Th>Name</Table.Th>
            {showNamespace && <Table.Th>Namespace</Table.Th>}
            <Table.Th w={110}>Phase</Table.Th>
            <Table.Th w={120}>State</Table.Th>
            <Table.Th>Source → target</Table.Th>
            <Table.Th w={80}>Tables</Table.Th>
            <Table.Th w={90}>Lag</Table.Th>
            <Table.Th w={110}>Retained WAL</Table.Th>
            <Table.Th w={130}>Observed</Table.Th>
          </Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          {rows.map((replication) => (
            <Table.Tr key={`${replication.namespace}/${replication.name}`}>
              <Table.Td>
                <Anchor
                  component={Link}
                  to={`/replications/${replication.namespace}/${replication.name}`}
                  fw={600}
                >
                  {replication.name}
                </Anchor>
              </Table.Td>
              {showNamespace && (
                <Table.Td>
                  <Text fz="xs" c="dimmed">
                    {replication.namespace}
                  </Text>
                </Table.Td>
              )}
              <Table.Td>
                <PhaseBadge phase={replication.phase} message={replication.message} />
              </Table.Td>
              <Table.Td>
                <StatePill state={replication.state} />
              </Table.Td>
              <Table.Td>
                <Group gap={6} wrap="nowrap">
                  <End end={replication.source} />
                  <IconArrowRight size={13} aria-label="to" style={{ flex: 'none', opacity: 0.6 }} />
                  <End end={replication.target} />
                </Group>
              </Table.Td>
              <Table.Td className="pgop-numeric">
                {replication.tables.ready}/{replication.tables.subscribed}
              </Table.Td>
              <Table.Td className="pgop-numeric">{replication.health?.lag ?? '—'}</Table.Td>
              <Table.Td className="pgop-numeric">{replication.health?.retained_wal ?? '—'}</Table.Td>
              <Table.Td>
                <RelativeTime value={replication.health?.observed_at} fz="sm" c="dimmed" />
              </Table.Td>
            </Table.Tr>
          ))}
        </Table.Tbody>
      </Table>
    </Table.ScrollContainer>
  )
}
