import { Group, Stack, Text } from '@mantine/core'

import { useHealth } from '../lib/queries'
import { Pill } from './Pill'

/**
 * The same `/readyz` answer the header pill summarises, opened out.
 *
 * On the overview it sits on the hero band rather than behind a popover: the
 * headline above it is only as trustworthy as the connection it was read
 * over, so the two belong in the same glance.
 */
export function HealthRows() {
  const { data } = useHealth()
  const ok = data?.status === 'ok'

  const rows = [
    { label: 'Kubernetes API', value: data?.kubernetes ? 'reachable' : 'unreachable' },
    ...Object.entries(data?.crds ?? {}).map(([plural, installed]) => ({
      label: plural,
      value: installed ? 'installed' : 'missing',
    })),
  ]

  return (
    <>
      <Group justify="space-between" gap="sm" wrap="nowrap">
        <Text className="pgop-eyebrow" c="var(--pgop-gold)">
          Readyz
        </Text>
        {/* Gold, not a phase tone: this reports on the console's own
            connection rather than on a resource's state. */}
        <Pill
          tone={data && !ok ? 'Failed' : 'Gold'}
          variant="outline"
          label={data ? (ok ? 'healthy' : 'degraded') : 'checking'}
        />
      </Group>

      <Stack gap={0} mt="md">
        {rows.map((row) => (
          <Group
            key={row.label}
            className="pgop-band-row"
            justify="space-between"
            wrap="nowrap"
            gap="md"
            py={11}
          >
            <Text ff="monospace" fz="sm" c="var(--pgop-on-band)" className="pgop-wrap">
              {row.label}
            </Text>
            <Text fz="xs" fw={600} c="var(--pgop-on-band-soft)" style={{ flex: 'none' }}>
              {row.value}
            </Text>
          </Group>
        ))}
      </Stack>

      {data?.detail && (
        <Text fz="xs" mt="sm" c="var(--pgop-on-band-soft)" className="pgop-wrap">
          {data.detail}
        </Text>
      )}
    </>
  )
}
