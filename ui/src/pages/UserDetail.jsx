import { Anchor, Code, Grid, Text } from '@mantine/core'
import { Link, useParams } from 'react-router-dom'

import { ConditionsTable } from '../components/ConditionsTable'
import { DetailGrid } from '../components/DetailGrid'
import { ErrorState } from '../components/ErrorState'
import { GrantsTable } from '../components/GrantsTable'
import { Page } from '../components/Page'
import { PageHeader } from '../components/PageHeader'
import { Panel } from '../components/Panel'
import { PhaseBadge } from '../components/PhaseBadge'
import { Pill } from '../components/Pill'
import { RelativeTime } from '../components/RelativeTime'
import { SecretPanel } from '../components/SecretPanel'
import { TableSkeleton } from '../components/TableSkeleton'
import { retentionTone } from '../lib/phase'
import { useUser } from '../lib/queries'

export function UserDetail() {
  const { namespace, name } = useParams()
  const { data: user, isPending, isError, error } = useUser(namespace, name)

  const crumbs = [{ label: 'Users', to: '/users' }, { label: namespace }, { label: name }]

  if (isError) {
    return (
      <Page>
        <PageHeader title={name} crumbs={crumbs} mono />
        <ErrorState error={error} />
      </Page>
    )
  }
  if (isPending || !user) {
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
        title={user.name}
        crumbs={crumbs}
        mono
        badge={<PhaseBadge phase={user.phase} size="lg" />}
        subtitle={user.message}
        actions={
          <Text fz="sm" c="dimmed">
            reconciled <RelativeTime value={user.last_reconciled_at} fz="sm" c="dimmed" />
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
                    <Pill
                      tone={user.login ? 'Ready' : 'Unknown'}
                      label={user.login ? 'LOGIN' : 'NOLOGIN'}
                    />
                  ),
                },
                {
                  label: 'Retention',
                  value: user.retention_policy && (
                    <Pill
                      tone={retentionTone(user.retention_policy)}
                      variant="outline"
                      label={user.retention_policy}
                    />
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
    </Page>
  )
}
