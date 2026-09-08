import { Alert, Anchor, Badge, Code, Grid, List, Stack, Table, Text } from '@mantine/core'
import { IconSettingsExclamation } from '@tabler/icons-react'
import { Link, useParams } from 'react-router-dom'

import { ConditionsTable } from '../components/ConditionsTable'
import { DetailGrid } from '../components/DetailGrid'
import { ErrorState } from '../components/ErrorState'
import { ExtensionList } from '../components/ExtensionList'
import { NoRows } from '../components/NoRows'
import { PageHeader } from '../components/PageHeader'
import { Panel } from '../components/Panel'
import { PhaseBadge } from '../components/PhaseBadge'
import { RelativeTime } from '../components/RelativeTime'
import { SchemaTable } from '../components/SchemaTable'
import { TableSkeleton } from '../components/TableSkeleton'
import { accessColor } from '../lib/phase'
import { useDatabase } from '../lib/queries'

export function DatabaseDetail() {
  const { namespace, name } = useParams()
  const { data: db, isPending, isError, error } = useDatabase(namespace, name)

  const crumbs = [
    { label: 'Databases', to: '/databases' },
    { label: namespace },
    { label: name },
  ]

  if (isError) {
    return (
      <>
        <PageHeader title={name} crumbs={crumbs} />
        <ErrorState error={error} />
      </>
    )
  }
  if (isPending || !db) {
    return (
      <>
        <PageHeader title={name} crumbs={crumbs} />
        <TableSkeleton rows={6} />
      </>
    )
  }

  return (
    <>
      <PageHeader
        title={db.name}
        crumbs={crumbs}
        badge={<PhaseBadge phase={db.phase} size="md" />}
        subtitle={db.message}
        actions={
          <Text fz="xs" c="dimmed">
            reconciled <RelativeTime value={db.last_reconciled_at} fz="xs" c="dimmed" />
          </Text>
        }
      />

      <Grid gutter="md">
        {db.denied_parameters.length > 0 && (
          <Grid.Col span={12}>
            <Alert
              color="amber"
              icon={<IconSettingsExclamation size={18} />}
              title="Parameters the managing role was refused"
            >
              <Text fz="xs" mb="xs">
                The rest of the database still converged. On RDS the master user cannot set
                superuser-only settings; PostgreSQL 15 added <Code fz="xs">GRANT … ON PARAMETER</Code>{' '}
                as the closest equivalent, and 14 has no mechanism at all.
              </Text>
              <List fz="xs" spacing={2}>
                {db.denied_parameters.map((entry) => (
                  <List.Item key={entry}>
                    <Text fz="xs" ff="monospace" className="pgop-wrap">
                      {entry}
                    </Text>
                  </List.Item>
                ))}
              </List>
            </Alert>
          </Grid.Col>
        )}

        <Grid.Col span={{ base: 12, md: 6 }}>
          <Panel title="Database" h="100%">
            <DetailGrid
              items={[
                { label: 'Namespace', value: db.namespace },
                {
                  label: 'Instance',
                  value: db.instance && (
                    <Anchor component={Link} to={`/instances/${db.instance}`} fz="sm">
                      {db.instance}
                    </Anchor>
                  ),
                },
                { label: 'Endpoint', value: db.endpoint && <Code fz="xs">{db.endpoint}</Code> },
                { label: 'Database name', value: <Code fz="xs">{db.database_name}</Code> },
                {
                  label: 'Retention',
                  value: db.retention_policy && (
                    <Badge
                      color={db.retention_policy === 'DROP' ? 'red' : 'slate'}
                      variant="outline"
                    >
                      {db.retention_policy}
                    </Badge>
                  ),
                },
                {
                  label: 'Generation',
                  value:
                    db.generation == null
                      ? null
                      : `${db.observed_generation ?? '—'} observed of ${db.generation}`,
                },
              ]}
            />
          </Panel>
        </Grid.Col>

        <Grid.Col span={{ base: 12, md: 6 }}>
          <Panel
            title="Group roles"
            description="NOLOGIN roles that hold every privilege. Users are granted membership, never the privileges themselves."
            h="100%"
          >
            <DetailGrid
              labelWidth={110}
              items={[
                {
                  label: 'OWNER',
                  value: db.roles.owner && (
                    <Badge color={accessColor('OWNER')} ff="monospace">
                      {db.roles.owner}
                    </Badge>
                  ),
                },
                {
                  label: 'RW',
                  value: db.roles.read_write && (
                    <Badge color={accessColor('RW')} ff="monospace">
                      {db.roles.read_write}
                    </Badge>
                  ),
                },
                {
                  label: 'RO',
                  value: db.roles.read_only && (
                    <Badge color={accessColor('RO')} ff="monospace">
                      {db.roles.read_only}
                    </Badge>
                  ),
                },
              ]}
            />
            <Text fz="xs" c="dimmed" mt="md">
              Extensions
            </Text>
            <Stack mt={6}>
              <ExtensionList database={db} />
            </Stack>
          </Panel>
        </Grid.Col>

        <Grid.Col span={12}>
          <Panel
            title="Schemas and effective grants"
            description="Read back from the server on every pass, so this is what the group roles actually hold."
          >
            <SchemaTable database={db} />
          </Panel>
        </Grid.Col>

        <Grid.Col span={12}>
          <Panel title="Conditions">
            <ConditionsTable conditions={db.conditions} />
          </Panel>
        </Grid.Col>

        <Grid.Col span={12}>
          <Panel
            title={`Users granted on this database (${db.users.length})`}
            description="Access is declared on the PostgresUser, not here."
          >
            {db.users.length ? (
              <Table.ScrollContainer minWidth={620} type="native">
                <Table fz="sm" verticalSpacing="xs">
                  <Table.Thead>
                    <Table.Tr>
                      <Table.Th>User</Table.Th>
                      <Table.Th>Login role</Table.Th>
                      <Table.Th>Access</Table.Th>
                      <Table.Th w={110}>Phase</Table.Th>
                    </Table.Tr>
                  </Table.Thead>
                  <Table.Tbody>
                    {db.users.map((user) => (
                      <Table.Tr key={`${user.namespace}/${user.name}`}>
                        <Table.Td>
                          <Anchor
                            component={Link}
                            to={`/users/${user.namespace}/${user.name}`}
                            fz="sm"
                          >
                            {user.namespace}/{user.name}
                          </Anchor>
                        </Table.Td>
                        <Table.Td>
                          <Text fz="xs" ff="monospace">
                            {user.username}
                          </Text>
                        </Table.Td>
                        <Table.Td>
                          {user.grants
                            .filter((grant) => grant.database === db.database_name)
                            .map((grant) => (
                              <Badge
                                key={grant.role}
                                color={accessColor(grant.role)}
                                size="sm"
                                mr={4}
                              >
                                {grant.role} → {grant.group_role}
                              </Badge>
                            ))}
                        </Table.Td>
                        <Table.Td>
                          <PhaseBadge phase={user.phase} message={user.message} />
                        </Table.Td>
                      </Table.Tr>
                    ))}
                  </Table.Tbody>
                </Table>
              </Table.ScrollContainer>
            ) : (
              <NoRows
                title="No users have access"
                description="Create a PostgresUser with an access entry naming this PostgresDB."
              />
            )}
          </Panel>
        </Grid.Col>
      </Grid>
    </>
  )
}
