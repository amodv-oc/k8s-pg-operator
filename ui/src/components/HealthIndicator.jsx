import { Badge, Group, Popover, Stack, Text } from '@mantine/core'
import { IconCircleCheck, IconCircleX } from '@tabler/icons-react'

import { useHealth } from '../lib/queries'

/**
 * `/readyz`, which is ready only when the Kubernetes API answers *and* all
 * three CRDs are installed. A missing CRD is reported separately from an
 * unreachable cluster because the fix is completely different.
 */
export function HealthIndicator() {
  const { data } = useHealth()
  const ok = data?.status === 'ok'
  const label = data ? (ok ? 'healthy' : 'degraded') : 'checking'

  return (
    <Popover width={340} position="bottom-end" withArrow shadow="md">
      <Popover.Target>
        <Badge
          color={data ? (ok ? 'emerald' : 'red') : 'zinc'}
          variant="light"
          style={{ cursor: 'pointer' }}
          leftSection={ok ? <IconCircleCheck size={13} /> : <IconCircleX size={13} />}
        >
          API {label}
        </Badge>
      </Popover.Target>
      <Popover.Dropdown>
        <Stack gap="xs">
          <Group justify="space-between">
            <Text fz="sm">Kubernetes API</Text>
            <Badge color={data?.kubernetes ? 'emerald' : 'red'} size="sm">
              {data?.kubernetes ? 'reachable' : 'unreachable'}
            </Badge>
          </Group>
          {Object.entries(data?.crds ?? {}).map(([plural, installed]) => (
            <Group key={plural} justify="space-between">
              <Text fz="sm" ff="monospace">
                {plural}
              </Text>
              <Badge color={installed ? 'emerald' : 'red'} size="sm">
                {installed ? 'installed' : 'missing'}
              </Badge>
            </Group>
          ))}
          {data?.detail && (
            <Text fz="xs" c="dimmed" className="pgop-wrap">
              {data.detail}
            </Text>
          )}
        </Stack>
      </Popover.Dropdown>
    </Popover>
  )
}
