import { Anchor, Badge, Code, Grid, Text } from '@mantine/core'
import { Link, useParams } from 'react-router-dom'

import { ConditionsTable } from '../components/ConditionsTable'
import { DetailGrid } from '../components/DetailGrid'
import { ErrorState } from '../components/ErrorState'
import { GrantsTable } from '../components/GrantsTable'
import { PageHeader } from '../components/PageHeader'
import { Panel } from '../components/Panel'
import { PhaseBadge } from '../components/PhaseBadge'
import { RelativeTime } from '../components/RelativeTime'
import { SecretPanel } from '../components/SecretPanel'
import { TableSkeleton } from '../components/TableSkeleton'
import { useUser } from '../lib/queries'

export function UserDetail() {
  const { namespace, name } = useParams()
  const { data: user, isPending, isError, error } = useUser(namespace, name)

  const crumbs = [{ label: 'Users', to: '/users' }, { label: namespace }, { label: name }]

  if (isError) {
    return (
      <>
        <PageHeader title={name} crumbs={crumbs} />
        <ErrorState error={error} />
      </>
    )
  }
  if (isPending || !user) {
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
        title={user.name}
        crumbs={crumbs}
        badge={<PhaseBadge phase={user.phase} size="md" />}
        subtitle={user.message}
        actions={
          <Text fz="xs" c="dimmed">
            reconciled <RelativeTime value={user.last_reconciled_at} fz="xs" c="dimmed" />
          </Text>
        }
      />

      <Grid gutter="md">
        <Grid.Col span={{ base: 12, md: 6 }}>
          <Panel title="Role" h="100%">
            <DetailGrid
              items={[
                { label: 'Namespace', value: user.namespace },
                { label: 'Login role', value: <Code fz="xs">{user.username}</Code> },
                {
                  label: 'Instance',
                  value: user.instance && (
                    <Anchor component={Link} to={`/instances/${user.instance}`} fz="sm">
                      {user.instance}
                    </Anchor>
                  ),
                },
                { label: 'Endpoint', value: user.endpoint && <Code fz="xs">{user.endpoint}</Code> },
                {
                  label: 'Login',
                  value: (
                    <Badge color={user.login ? 'emerald' : 'zinc'}>
                      {user.login ? 'LOGIN' : 'NOLOGIN'}
                    </Badge>
                  ),
                },
                {
                  label: 'Retention',
                  value: user.retention_policy && (
                    <Badge
                      color={user.retention_policy === 'DROP' ? 'red' : 'slate'}
                      variant="outline"
                    >
                      {user.retention_policy}
                    </Badge>
                  ),
                },
                {
                  label: 'Generation',
                  value:
                    user.generation == null
                      ? null
                      : `${user.observed_generation ?? '—'} observed of ${user.generation}`,
                },
              ]}
            />
          </Panel>
        </Grid.Col>

        <Grid.Col span={{ base: 12, md: 6 }}>
          <Panel title="Credentials" h="100%">
            <SecretPanel user={user} />
          </Panel>
        </Grid.Col>

        <Grid.Col span={12}>
          <Panel
            title="Access"
            description="Membership of a group role. Changing a level is a REVOKE and a GRANT, with no object-level churn."
          >
            <GrantsTable grants={user.grants} />
          </Panel>
        </Grid.Col>

        <Grid.Col span={12}>
          <Panel title="Conditions">
            <ConditionsTable conditions={user.conditions} />
          </Panel>
        </Grid.Col>
      </Grid>
    </>
  )
}
