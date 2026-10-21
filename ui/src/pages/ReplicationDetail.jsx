import { Alert, Anchor, Code, Grid, Group, SimpleGrid, Stack, Table, Text } from '@mantine/core'
import { IconShieldLock } from '@tabler/icons-react'
import { Link, useParams } from 'react-router-dom'

import { ConditionsTable } from '../components/ConditionsTable'
import { DetailGrid } from '../components/DetailGrid'
import { ErrorState } from '../components/ErrorState'
import { NoRows } from '../components/NoRows'
import { Page } from '../components/Page'
import { PageHeader } from '../components/PageHeader'
import { Panel } from '../components/Panel'
import { PhaseBadge } from '../components/PhaseBadge'
import { Pill } from '../components/Pill'
import { RelativeTime } from '../components/RelativeTime'
import { StatePill } from '../components/StatePill'
import { TableSkeleton } from '../components/TableSkeleton'
import { ThresholdMeter } from '../components/ThresholdMeter'
import { formatThreshold } from '../lib/format'
import { useReplication } from '../lib/queries'
import { SYNC_STATES, replicationTableRows } from '../lib/resources'

const mono = (value) => value && <Code fz="xs">{value}</Code>

function yesNo(value, { good = 'yes', bad = 'no' } = {}) {
  if (value === null || value === undefined) return null
  return <Pill tone={value ? 'Ready' : 'Failed'} label={value ? good : bad} />
}

function EndPanel({ title, end, extra }) {
  return (
    <Panel title={title} h="100%">
      <DetailGrid
        labelWidth={130}
        items={[
          {
            label: 'Instance',
            value: end.instance && (
              <Anchor component={Link} to={`/instances/${end.instance}`} fz="sm">
                {end.instance}
              </Anchor>
            ),
          },
          {
            label: 'PostgresDB',
            value: end.postgres_db && (
              <Anchor component={Link} to={`/databases/${end.postgres_db}`} fz="sm">
                {end.postgres_db}
              </Anchor>
            ),
          },
          { label: 'Database', value: mono(end.database) },
          ...extra,
        ]}
      />
    </Panel>
  )
}

function HealthPanel({ replication }) {
  const { health, thresholds } = replication
  if (!health) {
    return (
      <Panel title="Health">
        <NoRows
          title="Not observed yet"
          description="Lag, retained WAL and the apply worker are read on every pass once the subscription exists."
        />
      </Panel>
    )
  }
  const errors = [
    health.apply_error_count != null && `${health.apply_error_count} apply`,
    health.sync_error_count != null && `${health.sync_error_count} table-sync`,
  ].filter(Boolean)
  const conflicts = Object.entries(health.conflicts ?? {}).filter(([, count]) => count > 0)

  return (
    <Panel
      title="Health"
      description="Lag and retained WAL are read from the slot on the source; the worker and errors from the subscription on the target."
      actions={
        <Text fz="xs" c="dimmed">
          observed <RelativeTime value={health.observed_at} fz="xs" c="dimmed" />
        </Text>
      }
    >
      <SimpleGrid cols={{ base: 1, sm: 2 }} spacing="xl" mb="lg">
        <ThresholdMeter
          label="Lag"
          value={health.lag_bytes}
          display={health.lag}
          limit={thresholds.max_lag_bytes}
          limitDisplay={formatThreshold(thresholds.max_lag, thresholds.max_lag_bytes)}
        />
        <ThresholdMeter
          label="Retained WAL on the source"
          value={health.retained_wal_bytes}
          display={health.retained_wal}
          limit={thresholds.max_retained_wal_bytes}
          limitDisplay={formatThreshold(thresholds.max_retained_wal, thresholds.max_retained_wal_bytes)}
        />
      </SimpleGrid>
      <DetailGrid
        items={[
          { label: 'Slot in use', value: yesNo(health.slot_active, { bad: 'no consumer' }) },
          {
            label: 'No consumer since',
            value: health.inactive_since && (
              <Text fz="sm">
                <RelativeTime value={health.inactive_since} fz="sm" /> · stalls after{' '}
                {thresholds.max_slot_inactive}
              </Text>
            ),
          },
          { label: 'WAL status', value: mono(health.wal_status) },
          { label: 'Apply worker', value: yesNo(health.worker_running, { good: 'running', bad: 'not running' }) },
          {
            label: 'Last message',
            value: health.last_message_at && <RelativeTime value={health.last_message_at} fz="sm" />,
          },
          { label: 'Errors', value: errors.length ? errors.join(' · ') : null },
          {
            label: 'Conflicts',
            value: conflicts.length
              ? conflicts.map(([kind, count]) => `${count} ${kind.replaceAll('_', ' ')}`).join(' · ')
              : null,
          },
        ]}
      />
    </Panel>
  )
}

function TablesPanel({ replication }) {
  const rows = replicationTableRows(replication)
  const { tables } = replication
  return (
    <Panel
      title="Tables"
      description={`${tables.ready} of ${tables.subscribed} subscribed table(s) ready; ${tables.excluded} held back. A table is published only once both servers can take it.`}
      bleed={rows.length > 0}
    >
      {rows.length ? (
        <Table.ScrollContainer minWidth={720} type="native">
          <Table fz="sm" verticalSpacing="xs">
            <Table.Thead>
              <Table.Tr>
                <Table.Th w="28%">Table</Table.Th>
                <Table.Th w={180}>Standing</Table.Th>
                <Table.Th>Detail</Table.Th>
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {rows.map((row) => {
                const sync = row.sync ? SYNC_STATES[row.sync] : null
                return (
                  <Table.Tr key={row.table}>
                    <Table.Td>
                      <Text fz="xs" ff="monospace">
                        {row.table}
                      </Text>
                    </Table.Td>
                    <Table.Td>
                      <Group gap={4}>
                        {sync && <Pill tone={sync.tone} label={sync.label} message={sync.hint} />}
                        {!sync && !row.issues.length && <Pill tone="Pending" label="published" />}
                        {row.issues.map((issue) => (
                          <Pill key={issue.reason} tone="Drifted" label={issue.reason} />
                        ))}
                        {row.warnings.map((warning) => (
                          <Pill key={warning.reason} tone="Drifted" variant="outline" label={warning.reason} />
                        ))}
                      </Group>
                    </Table.Td>
                    <Table.Td>
                      {row.issues.length || row.warnings.length ? (
                        <Stack gap={4}>
                          {[...row.issues, ...row.warnings].map((entry, index) => (
                            <Text key={`${entry.reason}-${index}`} fz="xs" c="dimmed" className="pgop-wrap">
                              {entry.message}
                            </Text>
                          ))}
                        </Stack>
                      ) : (
                        <Text fz="xs" c="dimmed">
                          —
                        </Text>
                      )}
                    </Table.Td>
                  </Table.Tr>
                )
              })}
            </Table.Tbody>
          </Table>
        </Table.ScrollContainer>
      ) : (
        <NoRows title="No tables yet" description="Nothing selected exists in the source database yet." />
      )}
    </Panel>
  )
}

export function ReplicationDetail() {
  const { namespace, name } = useParams()
  const { data: replication, isPending, isError, error } = useReplication(namespace, name)
  const crumbs = [{ label: 'Replications', to: '/replications' }, { label: namespace }, { label: name }]

  if (isError) {
    return (
      <Page>
        <PageHeader title={name} crumbs={crumbs} mono />
        <ErrorState error={error} />
      </Page>
    )
  }
  if (isPending || !replication) {
    return (
      <Page>
        <PageHeader title={name} crumbs={crumbs} mono />
        <TableSkeleton rows={6} />
      </Page>
    )
  }

  const streaming = replication.conditions.find((c) => c.type === 'Streaming')
  const secret = replication.credentials_secret

  return (
    <Page>
      <PageHeader
        title={replication.name}
        crumbs={crumbs}
        mono
        badge={
          <Group gap={6}>
            <PhaseBadge phase={replication.phase} size="lg" />
            <StatePill state={replication.state} message={streaming?.message} size="lg" />
          </Group>
        }
        subtitle={replication.message}
        actions={
          <Text fz="sm" c="dimmed">
            reconciled <RelativeTime value={replication.last_reconciled_at} fz="sm" c="dimmed" />
          </Text>
        }
      />

      <Grid gutter="md">
        <Grid.Col span={12}>
          <HealthPanel replication={replication} />
        </Grid.Col>

        <Grid.Col span={{ base: 12, md: 6 }}>
          <EndPanel
            title="Source"
            end={replication.source}
            extra={[
              { label: 'Publication', value: mono(replication.publication) },
              { label: 'Slot', value: mono(replication.slot) },
              { label: 'Login', value: mono(replication.login) },
            ]}
          />
        </Grid.Col>

        <Grid.Col span={{ base: 12, md: 6 }}>
          <EndPanel
            title="Target"
            end={replication.target}
            extra={[
              { label: 'Subscription', value: mono(replication.subscription) },
              { label: 'Enabled', value: yesNo(replication.enabled, { bad: 'disabled' }) },
            ]}
          />
        </Grid.Col>

        <Grid.Col span={12}>
          <TablesPanel replication={replication} />
        </Grid.Col>

        <Grid.Col span={12}>
          <Panel title="Credentials">
            <DetailGrid
              labelWidth={130}
              items={[
                {
                  label: 'Secret',
                  value: secret ? mono([secret.namespace, secret.name].filter(Boolean).join('/')) : 'not created yet',
                },
                {
                  label: 'Keys',
                  value: replication.secret_keys.length ? (
                    <Group gap={6}>
                      {replication.secret_keys.map((key) => (
                        <Pill key={key} tone="RO" label={key} variant="outline" ff="monospace" />
                      ))}
                    </Group>
                  ) : null,
                },
                {
                  label: 'Login owner',
                  value:
                    replication.credentials_generated === false ? (
                      <Pill tone="OWNER" label="provided via credentialsSecretRef" />
                    ) : replication.credentials_generated ? (
                      <Pill tone="RW" label="generated by the operator" />
                    ) : null,
                },
              ]}
            />
            <Alert color="cream" icon={<IconShieldLock size={18} />} variant="light" mt="md">
              <Text fz="xs">
                This API never returns credential values or the subscription&apos;s connection
                string — only the Secret&apos;s name and keys.
              </Text>
            </Alert>
          </Panel>
        </Grid.Col>

        <Grid.Col span={12}>
          <Panel title="Conditions">
            <ConditionsTable conditions={replication.conditions} />
          </Panel>
        </Grid.Col>
      </Grid>
    </Page>
  )
}
