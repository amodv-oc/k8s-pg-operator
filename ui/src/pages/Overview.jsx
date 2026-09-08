import { Anchor, Group, List, SimpleGrid, Stack, Text } from '@mantine/core'
import {
  IconChecks,
  IconDatabase,
  IconAlertTriangle,
  IconServer2,
  IconUsers,
} from '@tabler/icons-react'
import { Link } from 'react-router-dom'

import { ErrorState } from '../components/ErrorState'
import { NoRows } from '../components/NoRows'
import { PageHeader } from '../components/PageHeader'
import { Panel } from '../components/Panel'
import { PhaseSummaryCard } from '../components/PhaseSummaryCard'
import { RelativeTime } from '../components/RelativeTime'
import { TableSkeleton } from '../components/TableSkeleton'
import { useOverview } from '../lib/queries'
import { parseAttention } from '../lib/resources'

export function Overview() {
  const { data, isPending, isError, error } = useOverview()

  return (
    <>
      <PageHeader
        title="Overview"
        subtitle="What the reconciler has written to resource status, across every kind."
        actions={
          data && (
            <Text fz="xs" c="dimmed">
              updated <RelativeTime value={data.generated_at} fz="xs" c="dimmed" />
            </Text>
          )
        }
      />

      {isError && <ErrorState error={error} />}

      {isPending && !isError && <TableSkeleton rows={5} />}

      {data && (
        <Stack gap="lg">
          <SimpleGrid cols={{ base: 1, xs: 3 }} spacing="md">
            <PhaseSummaryCard
              title="Instances"
              icon={<IconServer2 size={17} />}
              counts={data.instances}
              to="/instances"
            />
            <PhaseSummaryCard
              title="Databases"
              icon={<IconDatabase size={17} />}
              counts={data.databases}
              to="/databases"
            />
            <PhaseSummaryCard
              title="Users"
              icon={<IconUsers size={17} />}
              counts={data.users}
              to="/users"
            />
          </SimpleGrid>

          <Panel
            title="Needs attention"
            description="Anything not Ready or Paused, plus databases retaining orphaned objects."
          >
            {data.healthy ? (
              <NoRows
                icon={<IconChecks size={22} />}
                title="Everything is reconciled"
                description="No resource is reporting a problem and nothing is being retained as an orphan."
              />
            ) : (
              <List spacing="xs" fz="sm" listStyleType="none" withPadding={false}>
                {data.attention.map((entry) => {
                  const item = parseAttention(entry)
                  return (
                    <List.Item key={entry}>
                      <Group gap="xs" wrap="nowrap" align="flex-start">
                        <IconAlertTriangle
                          size={15}
                          style={{ marginTop: 3, flexShrink: 0 }}
                          color="var(--mantine-color-amber-6)"
                        />
                        <Text fz="sm" className="pgop-wrap">
                          {item.to ? (
                            <Anchor component={Link} to={item.to} fw={500}>
                              {item.ref}
                            </Anchor>
                          ) : null}{' '}
                          {item.text}
                        </Text>
                      </Group>
                    </List.Item>
                  )
                })}
              </List>
            )}
          </Panel>
        </Stack>
      )}
    </>
  )
}
