import { Group, Popover, Stack, Text } from '@mantine/core'
import { IconCircleCheck, IconCircleX } from '@tabler/icons-react'

import { useHealth } from '../lib/queries'
import { Pill } from './Pill'

/** The tone a readyz answer reads in: healthy, degraded, or not yet known. */
function healthTone(data) {
  if (!data) return 'Unknown'
  return data.status === 'ok' ? 'Ready' : 'Failed'
}

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
        <Pill
          tone={healthTone(data)}
          style={{ cursor: 'pointer' }}
          leftSection={ok ? <IconCircleCheck size={13} /> : <IconCircleX size={13} />}
          label={`API ${label}`}
        />
      </Popover.Target>
      <Popover.Dropdown>
        <Stack gap="xs">
          <Group justify="space-between">
            <Text fz="sm">Kubernetes API</Text>
            <Pill
              tone={data?.kubernetes ? 'Ready' : 'Failed'}
              label={data?.kubernetes ? 'reachable' : 'unreachable'}
            />
          </Group>
          {Object.entries(data?.crds ?? {}).map(([plural, installed]) => (
            <Group key={plural} justify="space-between">
              <Text fz="sm" ff="monospace">
                {plural}
              </Text>
              <Pill
                tone={installed ? 'Ready' : 'Failed'}
                label={installed ? 'installed' : 'missing'}
              />
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
