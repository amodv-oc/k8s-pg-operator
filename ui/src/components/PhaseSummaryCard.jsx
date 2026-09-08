import { Card, Group, Text, UnstyledButton } from '@mantine/core'
import { useNavigate } from 'react-router-dom'

import { COUNTS, phaseTone } from '../lib/phase'
import { Pill } from './Pill'

/**
 * A kind's phase breakdown. Every non-zero phase is a link into the matching
 * filtered list, because "3 Drifted" is only useful if you can see which three.
 */
export function PhaseSummaryCard({ title, icon, counts, to }) {
  const navigate = useNavigate()
  const shown = COUNTS.filter(({ key }) => counts?.[key] > 0)

  return (
    <Card
      className="pgop-tile"
      onClick={() => navigate(to)}
      role="link"
      tabIndex={0}
      onKeyDown={(event) => event.key === 'Enter' && navigate(to)}
    >
      <Group justify="space-between" align="flex-start" wrap="nowrap">
        <Group gap="xs" wrap="nowrap">
          <span className="pgop-kind-icon">{icon}</span>
          <Text fz="md" fw={600}>
            {title}
          </Text>
        </Group>
        <Text component="span" className="pgop-total">
          {counts?.total ?? 0}
        </Text>
      </Group>

      <Group gap="xs" mt="md">
        {shown.length ? (
          shown.map(({ key, label, phase }) => (
            <UnstyledButton
              key={key}
              title={`Show only ${label.toLowerCase()} ${title.toLowerCase()}`}
              onClick={(event) => {
                event.stopPropagation()
                navigate(`${to}?phase=${phase}`)
              }}
            >
              <Pill
                tone={phaseTone(phase)}
                size="md"
                label={
                  <span style={{ display: 'inline-flex', gap: 6, alignItems: 'baseline' }}>
                    <span className="pgop-numeric">{counts[key]}</span>
                    <span style={{ fontWeight: 500, opacity: 0.85 }}>{label}</span>
                  </span>
                }
              />
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
