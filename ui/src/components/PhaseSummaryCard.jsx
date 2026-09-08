import { Card, Group, Text, Title, UnstyledButton } from '@mantine/core'
import { useNavigate } from 'react-router-dom'

import { COUNTS, phaseColor } from '../lib/phase'

/**
 * A kind's phase breakdown. Every non-zero phase is a link into the matching
 * filtered list, because "3 Drifted" is only useful if you can see which three.
 */
export function PhaseSummaryCard({ title, icon, counts, to }) {
  const navigate = useNavigate()
  const shown = COUNTS.filter(({ key }) => counts?.[key] > 0)

  return (
    <Card className="pgop-tile" onClick={() => navigate(to)} role="link" tabIndex={0}
      onKeyDown={(event) => event.key === 'Enter' && navigate(to)}>
      <Group justify="space-between" align="flex-start" wrap="nowrap">
        <Group gap="xs">
          {icon}
          <Title order={6}>{title}</Title>
        </Group>
        <Text fz="2xl" fw={600} className="pgop-numeric" lh={1}>
          {counts?.total ?? 0}
        </Text>
      </Group>

      <Group gap="xs" mt="md">
        {shown.length ? (
          shown.map(({ key, label, phase }) => (
            <UnstyledButton
              key={key}
              onClick={(event) => {
                event.stopPropagation()
                navigate(`${to}?phase=${phase}`)
              }}
            >
              <Group gap={6} wrap="nowrap">
                <Text fz="sm" fw={600} className="pgop-numeric" c={phaseColor(phase)}>
                  {counts[key]}
                </Text>
                <Text fz="xs" c="dimmed">
                  {label}
                </Text>
              </Group>
            </UnstyledButton>
          ))
        ) : (
          <Text fz="xs" c="dimmed">
            none registered
          </Text>
        )}
      </Group>
    </Card>
  )
}
