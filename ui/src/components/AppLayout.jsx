import {
  ActionIcon,
  AppShell,
  Box,
  Burger,
  Group,
  Stack,
  Switch,
  Text,
  Tooltip,
  UnstyledButton,
  useComputedColorScheme,
  useMantineColorScheme,
} from '@mantine/core'
import { useDisclosure } from '@mantine/hooks'
import {
  IconBook,
  IconDatabase,
  IconGauge,
  IconMoon,
  IconServer2,
  IconSun,
  IconUsers,
} from '@tabler/icons-react'
import { NavLink as RouterNavLink, Outlet, useLocation } from 'react-router-dom'

import { config } from '../lib/config'
import { useOverview } from '../lib/queries'
import { useRefresh } from '../lib/refresh'
import { HealthIndicator } from './HealthIndicator'
import { Pill } from './Pill'
import { RefreshFab } from './RefreshFab'

const SECTIONS = [
  { to: '/', label: 'Overview', icon: IconGauge, exact: true },
  { to: '/instances', label: 'Instances', icon: IconServer2, kind: 'instances' },
  { to: '/databases', label: 'Databases', icon: IconDatabase, kind: 'databases' },
  { to: '/users', label: 'Users', icon: IconUsers, kind: 'users' },
]

/** The database mark the wordmark sits against. */
function LogoMark() {
  return (
    <span className="pgop-logo">
      <svg
        width="19"
        height="19"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden="true"
      >
        <path d="M4 6a8 3 0 1 0 16 0a8 3 0 1 0 -16 0" />
        <path d="M4 6v6a8 3 0 0 0 16 0v-6" />
        <path d="M4 12v6a8 3 0 0 0 16 0v-6" />
      </svg>
    </span>
  )
}

function ColorSchemeToggle() {
  const { setColorScheme } = useMantineColorScheme()
  const computed = useComputedColorScheme('light', { getInitialValueInEffect: true })
  const next = computed === 'dark' ? 'light' : 'dark'

  return (
    <Tooltip label={`Switch to ${next} mode`}>
      <ActionIcon
        onClick={() => setColorScheme(next)}
        aria-label="Toggle colour scheme"
        size="lg"
        radius="xl"
      >
        {computed === 'dark' ? <IconSun size={17} /> : <IconMoon size={17} />}
      </ActionIcon>
    </Tooltip>
  )
}

export function AppLayout() {
  const [opened, { toggle, close }] = useDisclosure(false)
  const { live, setLive, intervalMs } = useRefresh()
  const { data: overview } = useOverview()
  const location = useLocation()
  const cluster = config().clusterLabel

  return (
    <AppShell
      header={{ height: 72 }}
      navbar={{ width: 232, breakpoint: 'sm', collapsed: { mobile: !opened } }}
      padding={0}
    >
      <AppShell.Header className="pgop-header" withBorder={false}>
        <Group h="100%" px={{ base: 'md', md: 'xl' }} justify="space-between" gap="md" wrap="nowrap">
          <Group gap={14} wrap="nowrap" style={{ flex: 'none' }}>
            <Burger opened={opened} onClick={toggle} hiddenFrom="sm" size="sm" />
            <LogoMark />
            <Group gap={10} align="baseline" wrap="nowrap" visibleFrom="xs">
              <Text ff="monospace" fz="md" fw={500} style={{ whiteSpace: 'nowrap' }}>
                pg-operator
              </Text>
              {/* Gold, and the only gold in the chrome: which cluster this tab
                  is pointed at is the one fact worth a ceremony colour. */}
              {cluster && (
                <Pill tone="Gold" variant="outline" label={cluster} tt="uppercase" lts="0.1em" />
              )}
            </Group>
          </Group>

          <Group gap="xs" wrap="nowrap" justify="flex-end">
            <Box visibleFrom="sm">
              <HealthIndicator />
            </Box>
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
                size="sm"
                label="Live"
                labelPosition="left"
                aria-label="Toggle auto-refresh"
                styles={{ label: { fontSize: '0.75rem', fontWeight: 600 } }}
              />
            </Tooltip>
            <Tooltip label="OpenAPI docs">
              <ActionIcon
                component="a"
                href="/docs"
                target="_blank"
                rel="noopener noreferrer"
                aria-label="OpenAPI docs"
                size="lg"
                radius="xl"
              >
                <IconBook size={17} />
              </ActionIcon>
            </Tooltip>
            <ColorSchemeToggle />
          </Group>
        </Group>
      </AppShell.Header>

      <AppShell.Navbar className="pgop-rail" p="md" withBorder={false}>
        <Stack gap={4}>
          {SECTIONS.map(({ to, label, icon: Icon, kind, exact }) => {
            const active = exact ? location.pathname === to : location.pathname.startsWith(to)
            const total = kind ? overview?.[kind]?.total : undefined
            return (
              <UnstyledButton
                key={to}
                component={RouterNavLink}
                to={to}
                end={exact}
                onClick={close}
                className="pgop-nav-item"
                data-active={active || undefined}
              >
                <Icon size={17} style={{ flex: 'none' }} />
                <span style={{ flex: 1 }}>{label}</span>
                {total !== undefined && (
                  <span className="pgop-numeric" style={{ fontSize: '0.75rem', opacity: 0.75 }}>
                    {total}
                  </span>
                )}
              </UnstyledButton>
            )
          })}
        </Stack>

        {/* What the reconciler is doing, stated once where it cannot be
            mistaken for a resource's own status. */}
        <Box className="pgop-note">
          <Text className="pgop-eyebrow" c="dimmed">
            Reconciler
          </Text>
          <Text fz="sm" c="dimmed" mt="xs" style={{ textWrap: 'pretty' }}>
            {live
              ? `Polling every ${Math.round(intervalMs / 1000)}s.`
              : 'Auto-refresh is paused; the figures are as of the last pass.'}
          </Text>
        </Box>
      </AppShell.Navbar>

      <AppShell.Main>
        <Outlet />
        <RefreshFab />
      </AppShell.Main>
    </AppShell>
  )
}
