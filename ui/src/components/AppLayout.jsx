import {
  ActionIcon,
  AppShell,
  Badge,
  Burger,
  Code,
  Group,
  NavLink,
  Stack,
  Switch,
  Text,
  Tooltip,
  useComputedColorScheme,
  useMantineColorScheme,
} from '@mantine/core'
import { useDisclosure } from '@mantine/hooks'
import {
  IconBook,
  IconDatabase,
  IconGauge,
  IconMoon,
  IconRefresh,
  IconServer2,
  IconSun,
  IconUsers,
} from '@tabler/icons-react'
import { useIsFetching, useQueryClient } from '@tanstack/react-query'
import { NavLink as RouterNavLink, Outlet, useLocation } from 'react-router-dom'

import { config } from '../lib/config'
import { useOverview } from '../lib/queries'
import { useRefresh } from '../lib/refresh'
import { HealthIndicator } from './HealthIndicator'

const SECTIONS = [
  { to: '/', label: 'Overview', icon: IconGauge, exact: true },
  { to: '/instances', label: 'Instances', icon: IconServer2, kind: 'instances' },
  { to: '/databases', label: 'Databases', icon: IconDatabase, kind: 'databases' },
  { to: '/users', label: 'Users', icon: IconUsers, kind: 'users' },
]

function ColorSchemeToggle() {
  const { setColorScheme } = useMantineColorScheme()
  const computed = useComputedColorScheme('light', { getInitialValueInEffect: true })
  const next = computed === 'dark' ? 'light' : 'dark'

  return (
    <Tooltip label={`Switch to ${next} mode`}>
      <ActionIcon onClick={() => setColorScheme(next)} aria-label="Toggle colour scheme">
        {computed === 'dark' ? <IconSun size={17} /> : <IconMoon size={17} />}
      </ActionIcon>
    </Tooltip>
  )
}

export function AppLayout() {
  const [opened, { toggle, close }] = useDisclosure(false)
  const { live, setLive, intervalMs } = useRefresh()
  const queryClient = useQueryClient()
  const fetching = useIsFetching() > 0
  const { data: overview } = useOverview()
  const location = useLocation()
  const cluster = config().clusterLabel

  return (
    <AppShell
      header={{ height: 56 }}
      navbar={{ width: 210, breakpoint: 'sm', collapsed: { mobile: !opened } }}
      padding="lg"
    >
      <AppShell.Header>
        <Group h="100%" px="md" justify="space-between" wrap="nowrap">
          <Group gap="sm" wrap="nowrap">
            <Burger opened={opened} onClick={toggle} hiddenFrom="sm" size="sm" />
            <Text fw={600} fz="sm" ff="monospace">
              pg-operator
            </Text>
            {cluster && (
              <Badge color="slate" variant="outline" size="sm">
                {cluster}
              </Badge>
            )}
          </Group>

          <Group gap="sm" wrap="nowrap">
            <HealthIndicator />
            <Tooltip
              label={
                live
                  ? `Auto-refreshing every ${Math.round(intervalMs / 1000)}s`
                  : 'Auto-refresh paused'
              }
            >
              <Switch
                checked={live}
                onChange={(event) => setLive(event.currentTarget.checked)}
                size="xs"
                label="Live"
                labelPosition="left"
                aria-label="Toggle auto-refresh"
              />
            </Tooltip>
            <Tooltip label="Refresh now">
              <ActionIcon
                onClick={() => queryClient.invalidateQueries()}
                loading={fetching}
                aria-label="Refresh now"
              >
                <IconRefresh size={17} />
              </ActionIcon>
            </Tooltip>
            <Tooltip label="OpenAPI docs">
              <ActionIcon
                component="a"
                href="/docs"
                target="_blank"
                rel="noopener noreferrer"
                aria-label="OpenAPI docs"
              >
                <IconBook size={17} />
              </ActionIcon>
            </Tooltip>
            <ColorSchemeToggle />
          </Group>
        </Group>
      </AppShell.Header>

      <AppShell.Navbar p="sm">
        <Stack gap={2}>
          {SECTIONS.map(({ to, label, icon: Icon, kind, exact }) => {
            const active = exact ? location.pathname === to : location.pathname.startsWith(to)
            const total = kind ? overview?.[kind]?.total : undefined
            return (
              <NavLink
                key={to}
                component={RouterNavLink}
                to={to}
                end={exact}
                label={label}
                active={active}
                onClick={close}
                variant="light"
                leftSection={<Icon size={17} />}
                rightSection={
                  total === undefined ? null : (
                    <Code fz="xs" className="pgop-numeric">
                      {total}
                    </Code>
                  )
                }
              />
            )
          })}
        </Stack>
      </AppShell.Navbar>

      <AppShell.Main>
        <Outlet />
      </AppShell.Main>
    </AppShell>
  )
}
