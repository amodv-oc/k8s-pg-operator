import { Anchor, Group, SimpleGrid, Stack, Text } from '@mantine/core'
import { IconAlertTriangle, IconChecks, IconDatabase, IconServer2, IconUsers } from '@tabler/icons-react'
import { Link } from 'react-router-dom'

import { ErrorState } from '../components/ErrorState'
import { HealthRows } from '../components/HealthRows'
import { HeroBand } from '../components/HeroBand'
import { NoRows } from '../components/NoRows'
import { Page } from '../components/Page'
import { Panel } from '../components/Panel'
import { PhaseSummaryCard } from '../components/PhaseSummaryCard'
import { PhaseBadge } from '../components/PhaseBadge'
import { RelativeTime } from '../components/RelativeTime'
import { TableSkeleton } from '../components/TableSkeleton'
import { phaseTone } from '../lib/phase'
import { useOverview } from '../lib/queries'
import { parseAttention } from '../lib/resources'

/**
 * The one sentence the band exists to say.
 *
 * Deliberately the count of *entries* rather than of resources: one database
 * can appear once however many objects it is retaining, which is what the list
 * below shows, so the two numbers have to agree.
 */
function headline(data) {
  if (!data) return 'Reading resource status…'
  const total = (data.instances?.total ?? 0) + (data.databases?.total ?? 0) + (data.users?.total ?? 0)
  if (!total) return 'Nothing is registered yet.'
  if (data.healthy) {
    return total === 1
      ? 'The one registered resource is reconciled.'
      : `All ${total} resources are reconciled.`
  }
  return `${data.attention.length} of ${total} resources need attention.`
}

export function Overview() {
  const { data, isPending, isError, error } = useOverview()

  return (
    <>
      <HeroBand
        eyebrow="Overview"
        headline={headline(data)}
        subtitle="What the reconciler has written to resource status, across every kind."
        meta={
          data && (
            <>
              updated <RelativeTime value={data.generated_at} fz="sm" c="var(--pgop-on-band-soft)" />
            </>
          )
        }
        aside={<HealthRows />}
      />

      <Page>
        {isError && <ErrorState error={error} />}

        {isPending && !isError && <TableSkeleton rows={5} />}

        {data && (
          <Stack gap="lg">
            <SimpleGrid cols={{ base: 1, sm: 2, md: 3 }} spacing="md">
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
              bleed={!data.healthy}
            >
              {data.healthy ? (
                <NoRows
                  icon={<IconChecks size={22} />}
                  title="Everything is reconciled"
                  description="No resource is reporting a problem and nothing is being retained as an orphan."
                />
              ) : (
                <Stack gap={0}>
                  {data.attention.map((entry) => {
                    const item = parseAttention(entry)
                    return (
                      <Group
                        key={entry}
                        className="pgop-row"
                        align="flex-start"
                        wrap="nowrap"
                        gap={14}
                        px="lg"
                        py="md"
                      >
                        <IconAlertTriangle
                          size={16}
                          style={{
                            flex: 'none',
                            marginTop: 2,
                            color: `var(--pgop-tone-${phaseTone(item.phase)}-fg)`,
                          }}
                        />
                        <Text fz="sm" className="pgop-wrap" style={{ flex: 1, minWidth: 0 }}>
                          {item.to ? (
                            <Anchor component={Link} to={item.to} fw={500} ff="monospace" fz="xs">
                              {item.ref}
                            </Anchor>
                          ) : null}{' '}
                          {item.text}
                        </Text>
                        {item.phase && (
                          <span style={{ flex: 'none' }}>
                            <PhaseBadge phase={item.phase} />
                          </span>
                        )}
                      </Group>
                    )
                  })}
                </Stack>
              )}
            </Panel>
          </Stack>
        )}
      </Page>
    </>
  )
}
