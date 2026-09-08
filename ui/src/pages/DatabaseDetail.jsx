import { Alert, Anchor, Code, Grid, List, Stack, Table, Text } from '@mantine/core'
import { IconSettingsExclamation } from '@tabler/icons-react'
import { Link, useParams } from 'react-router-dom'

import { ConditionsTable } from '../components/ConditionsTable'
import { DetailGrid } from '../components/DetailGrid'
import { ErrorState } from '../components/ErrorState'
import { ExtensionList } from '../components/ExtensionList'
import { NoRows } from '../components/NoRows'
import { Page } from '../components/Page'
import { PageHeader } from '../components/PageHeader'
import { Panel } from '../components/Panel'
import { PhaseBadge } from '../components/PhaseBadge'
import { Pill } from '../components/Pill'
import { RelativeTime } from '../components/RelativeTime'
import { SchemaTable } from '../components/SchemaTable'
import { TableSkeleton } from '../components/TableSkeleton'
import { accessTone, retentionTone } from '../lib/phase'
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
      <Page>
        <PageHeader title={name} crumbs={crumbs} mono />
        <ErrorState error={error} />
      </Page>
    )
  }
  if (isPending || !db) {
    return (
      <Page>
        <PageHeader title={name} crumbs={crumbs} mono />
        <TableSkeleton rows={6} />
      </Page>
    )
  }

  return (
    <Page>
      <PageHeader
        title={db.name}
        crumbs={crumbs}
        mono
        badge={<PhaseBadge phase={db.phase} size="lg" />}
        subtitle={db.message}
        actions={
          <Text fz="sm" c="dimmed">
            reconciled <RelativeTime value={db.last_reconciled_at} fz="sm" c="dimmed" />
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
                    <Pill
                      tone={retentionTone(db.retention_policy)}
                      variant="outline"
                      label={db.retention_policy}
                    />
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
                    <Pill tone="OWNER" label={db.roles.owner} ff="monospace" />
                  ),
                },
                {
                  label: 'RW',
                  value: db.roles.read_write && (
                    <Pill tone="RW" label={db.roles.read_write} ff="monospace" />
                  ),
                },
                {
                  label: 'RO',
                  value: db.roles.read_only && (
                    <Pill tone="RO" label={db.roles.read_only} ff="monospace" />
                  ),
                },
              ]}
            />
            <Text className="pgop-eyebrow" c="dimmed" mt="lg">
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
                              <Pill
                                key={grant.role}
                                tone={accessTone(grant.role)}
                                mr={4}
                                label={`${grant.role} → ${grant.group_role}`}
                              />
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
    </Page>
  )
}
