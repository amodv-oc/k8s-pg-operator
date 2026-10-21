import { Alert, Anchor, Code, Grid, List, Stack, Table, Text } from '@mantine/core'
import { IconInfoCircle } from '@tabler/icons-react'
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
import { ReplicationsTable } from '../components/ReplicationsTable'
import { TableSkeleton } from '../components/TableSkeleton'
import { accessTone, retentionTone } from '../lib/phase'
import { useInstance } from '../lib/queries'

/**
 * What a replication on this server would need, read from it on every pass.
 * Informational: nothing here makes the instance itself unhealthy.
 */
function ReplicationReadiness({ readiness }) {
  if (!readiness) {
    return <Text c="dimmed" fz="sm">Not reported yet.</Text>
  }
  const unlimited = readiness.max_slot_wal_keep_size === '-1'
  return (
    <Stack gap="md">
      <DetailGrid
        labelWidth={190}
        items={[
          {
            label: 'wal_level',
            value: (
              <Pill
                tone={readiness.wal_level === 'logical' ? 'Ready' : 'Drifted'}
                label={readiness.wal_level}
                ff="monospace"
              />
            ),
          },
          { label: 'Can create subscriptions', value: readiness.create_subscription },
          { label: 'Can grant streaming', value: readiness.replication_grant },
          {
            label: 'Replication slots',
            value:
              readiness.max_replication_slots != null &&
              `${readiness.replication_slots_in_use} of ${readiness.max_replication_slots} in use`,
          },
          { label: 'max_wal_senders', value: readiness.max_wal_senders },
          {
            label: 'max_slot_wal_keep_size',
            value: (
              <Text fz="sm">
                <Code fz="xs">{readiness.max_slot_wal_keep_size}</Code>
                {unlimited && ' — unlimited: a stalled subscriber can fill the disk'}
              </Text>
            ),
          },
        ]}
      />
      {readiness.issues.length > 0 && (
        <Alert color="amber" icon={<IconInfoCircle size={18} />} title="Before this server can replicate">
          <List fz="xs" spacing={4}>
            {readiness.issues.map((issue) => (
              <List.Item key={issue}>{issue}</List.Item>
            ))}
          </List>
        </Alert>
      )}
    </Stack>
  )
}

export function InstanceDetail() {
  const { name } = useParams()
  const { data: instance, isPending, isError, error } = useInstance(name)

  if (isError) {
    return (
      <Page>
        <PageHeader title={name} crumbs={[{ label: 'Instances', to: '/instances' }]} mono />
        <ErrorState error={error} />
      </Page>
    )
  }
  if (isPending || !instance) {
    return (
      <Page>
        <PageHeader title={name} crumbs={[{ label: 'Instances', to: '/instances' }]} mono />
        <TableSkeleton rows={6} />
      </Page>
    )
  }

  return (
    <Page>
      <PageHeader
        title={instance.name}
        crumbs={[{ label: 'Instances', to: '/instances' }, { label: instance.name }]}
        mono
        badge={<PhaseBadge phase={instance.phase} size="lg" />}
        subtitle={instance.message}
        actions={
          <Text fz="sm" c="dimmed">
            connected <RelativeTime value={instance.last_connected_at} fz="sm" c="dimmed" />
          </Text>
        }
      />

      <Grid gutter="md">
        <Grid.Col span={{ base: 12, md: 6 }}>
          <Panel title="Server" h="100%">
            <DetailGrid
              items={[
                { label: 'Endpoint', value: <Code fz="xs">{instance.endpoint}</Code> },
                { label: 'Version', value: instance.server_version },
                {
                  // `serverVersionText` is current_setting('server_version'),
                  // which normally reads the same as the parsed version. Shown
                  // only when it says something the row above does not.
                  label: 'Version string',
                  value:
                    instance.server_version_text === instance.server_version
                      ? null
                      : instance.server_version_text,
                },
                { label: 'SSL mode', value: <Code fz="xs">{instance.ssl_mode}</Code> },
                {
                  label: 'Retention',
                  value: instance.retention_policy && (
                    <Pill
                      tone={retentionTone(instance.retention_policy)}
                      variant="outline"
                      label={instance.retention_policy}
                    />
                  ),
                },
              ]}
            />
          </Panel>
        </Grid.Col>

        <Grid.Col span={{ base: 12, md: 6 }}>
          <Panel
            title="Management credentials"
            description="The only role the operator uses on this server."
            h="100%"
          >
            <DetailGrid
              items={[
                { label: 'Managing role', value: instance.managing_role },
                { label: 'Privileges', value: instance.privileges },
                { label: 'Credentials Secret', value: instance.credentials_secret },
                {
                  label: 'PushSecret CRD',
                  value:
                    instance.push_secrets_available === null ||
                    instance.push_secrets_available === undefined ? null : (
                      <Pill
                        tone={instance.push_secrets_available ? 'Ready' : 'Unknown'}
                        label={instance.push_secrets_available ? 'available' : 'not installed'}
                      />
                    ),
                },
                {
                  label: 'Generation',
                  value:
                    instance.generation == null
                      ? null
                      : `${instance.observed_generation ?? '—'} observed of ${instance.generation}`,
                },
              ]}
            />
          </Panel>
        </Grid.Col>

        <Grid.Col span={12}>
          <Panel
            title="Logical replication"
            description="What a PostgresReplication on this server would need. On RDS these live in the parameter group, not here."
          >
            <ReplicationReadiness readiness={instance.logical_replication} />
          </Panel>
        </Grid.Col>

        <Grid.Col span={12}>
          <Panel title="Conditions">
            <ConditionsTable conditions={instance.conditions} />
          </Panel>
        </Grid.Col>

        <Grid.Col span={{ base: 12, md: 6 }}>
          <Panel title={`Databases (${instance.database_list.length})`} h="100%">
            {instance.database_list.length ? (
              <Table fz="sm" verticalSpacing="xs">
                <Table.Tbody>
                  {instance.database_list.map((db) => (
                    <Table.Tr key={`${db.namespace}/${db.name}`}>
                      <Table.Td>
                        <Anchor
                          component={Link}
                          to={`/databases/${db.namespace}/${db.name}`}
                          fz="sm"
                        >
                          {db.namespace}/{db.name}
                        </Anchor>
                      </Table.Td>
                      <Table.Td w={110}>
                        <PhaseBadge phase={db.phase} message={db.message} />
                      </Table.Td>
                    </Table.Tr>
                  ))}
                </Table.Tbody>
              </Table>
            ) : (
              <NoRows title="No databases on this instance" />
            )}
          </Panel>
        </Grid.Col>

        <Grid.Col span={{ base: 12, md: 6 }}>
          <Panel title={`Users (${instance.user_list.length})`} h="100%">
            {instance.user_list.length ? (
              <Table fz="sm" verticalSpacing="xs">
                <Table.Tbody>
                  {instance.user_list.map((user) => (
                    <Table.Tr key={`${user.namespace}/${user.name}`}>
                      <Table.Td>
                        <Anchor component={Link} to={`/users/${user.namespace}/${user.name}`} fz="sm">
                          {user.namespace}/{user.name}
                        </Anchor>
                      </Table.Td>
                      <Table.Td>
                        <Stack gap={2}>
                          {user.grants.map((grant) => (
                            <Pill
                              key={`${grant.database}/${grant.role}`}
                              tone={accessTone(grant.role)}
                              label={`${grant.role} · ${grant.database}`}
                            />
                          ))}
                        </Stack>
                      </Table.Td>
                      <Table.Td w={110}>
                        <PhaseBadge phase={user.phase} message={user.message} />
                      </Table.Td>
                    </Table.Tr>
                  ))}
                </Table.Tbody>
              </Table>
            ) : (
              <NoRows title="No users on this instance" />
            )}
          </Panel>
        </Grid.Col>

        <Grid.Col span={12}>
          <Panel
            title={`Replications (${instance.replication_list.length})`}
            description="From or into a database on this instance."
            bleed={instance.replication_list.length > 0}
          >
            {instance.replication_list.length ? (
              <ReplicationsTable rows={instance.replication_list} />
            ) : (
              <NoRows title="No replications use this instance" />
            )}
          </Panel>
        </Grid.Col>
      </Grid>
    </Page>
  )
}
