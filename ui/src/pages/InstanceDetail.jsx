import { Anchor, Code, Grid, Stack, Table, Text } from '@mantine/core'
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
import { TableSkeleton } from '../components/TableSkeleton'
import { accessTone, retentionTone } from '../lib/phase'
import { useInstance } from '../lib/queries'

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
      </Grid>
    </Page>
  )
}
