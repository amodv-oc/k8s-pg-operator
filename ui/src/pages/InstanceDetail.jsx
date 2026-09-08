import { Anchor, Badge, Code, Grid, Stack, Table, Text } from '@mantine/core'
import { Link, useParams } from 'react-router-dom'

import { ConditionsTable } from '../components/ConditionsTable'
import { DetailGrid } from '../components/DetailGrid'
import { ErrorState } from '../components/ErrorState'
import { NoRows } from '../components/NoRows'
import { PageHeader } from '../components/PageHeader'
import { Panel } from '../components/Panel'
import { PhaseBadge } from '../components/PhaseBadge'
import { RelativeTime } from '../components/RelativeTime'
import { TableSkeleton } from '../components/TableSkeleton'
import { accessColor } from '../lib/phase'
import { useInstance } from '../lib/queries'

export function InstanceDetail() {
  const { name } = useParams()
  const { data: instance, isPending, isError, error } = useInstance(name)

  if (isError) {
    return (
      <>
        <PageHeader title={name} crumbs={[{ label: 'Instances', to: '/instances' }]} />
        <ErrorState error={error} />
      </>
    )
  }
  if (isPending || !instance) {
    return (
      <>
        <PageHeader title={name} crumbs={[{ label: 'Instances', to: '/instances' }]} />
        <TableSkeleton rows={6} />
      </>
    )
  }

  return (
    <>
      <PageHeader
        title={instance.name}
        crumbs={[{ label: 'Instances', to: '/instances' }, { label: instance.name }]}
        badge={<PhaseBadge phase={instance.phase} size="md" />}
        subtitle={instance.message}
        actions={
          <Text fz="xs" c="dimmed">
            connected <RelativeTime value={instance.last_connected_at} fz="xs" c="dimmed" />
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
                    <Badge
                      color={instance.retention_policy === 'DROP' ? 'red' : 'slate'}
                      variant="outline"
                    >
                      {instance.retention_policy}
                    </Badge>
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
                      <Badge color={instance.push_secrets_available ? 'emerald' : 'zinc'}>
                        {instance.push_secrets_available ? 'available' : 'not installed'}
                      </Badge>
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
                            <Badge
                              key={`${grant.database}/${grant.role}`}
                              color={accessColor(grant.role)}
                              size="sm"
                            >
                              {grant.role} · {grant.database}
                            </Badge>
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
    </>
  )
}
