import { Group, Stack, Text, Tooltip } from '@mantine/core'
import { IconAlertOctagon, IconAlertTriangle, IconCircleCheck } from '@tabler/icons-react'

/**
 * A replication's lag recovers only once below this share of its threshold -
 * the operator's own hysteresis - so the meter warns from the same point.
 */
const RECOVERY = 0.8

/**
 * Severity as a status scale: good, warning, serious. Never colour alone - each
 * carries an icon and a word, and the measurement itself is plain text.
 */
const SEVERITY = {
  within: { tone: 'Ready', icon: IconCircleCheck, word: 'within threshold' },
  near: { tone: 'Drifted', icon: IconAlertTriangle, word: 'near threshold' },
  over: { tone: 'Degraded', icon: IconAlertOctagon, word: 'over threshold' },
}

/**
 * One measurement against its alerting threshold - a meter, not a chart.
 *
 * The fill carries the severity and the unfilled track is the same tone a step
 * lighter, so the state reads across the whole bar. A value over its threshold
 * fills the track and says by how much, rather than drawing past the end.
 */
export function ThresholdMeter({ label, value, display, limit, limitDisplay }) {
  if (value === null || value === undefined) {
    return (
      <Stack gap={6}>
        <Text fz="sm" c="dimmed">
          {label}
        </Text>
        <Text fz="sm" c="dimmed">
          not measured yet
        </Text>
      </Stack>
    )
  }

  const ratio = limit > 0 ? value / limit : 0
  const severity = ratio > 1 ? 'over' : ratio > RECOVERY ? 'near' : 'within'
  const { tone, icon: Icon, word } = SEVERITY[severity]
  const percent = Math.round(ratio * 100)

  return (
    <Stack gap={6}>
      <Group justify="space-between" align="baseline" wrap="nowrap">
        <Text fz="sm" c="dimmed">
          {label}
        </Text>
        <Group gap={6} wrap="nowrap">
          <Icon size={14} aria-hidden style={{ color: `var(--pgop-tone-${tone}-fg)` }} />
          <Text fz="xs" c="dimmed">
            {word}
          </Text>
        </Group>
      </Group>
      <Text fz="xl" fw={600} lh={1.2}>
        {display}{' '}
        <Text span fz="sm" fw={400} c="dimmed">
          of {limitDisplay}
          {ratio > 1 && ` · ${(ratio).toFixed(ratio >= 10 ? 0 : 1)}× the threshold`}
        </Text>
      </Text>
      <Tooltip
        label={`${Number(value).toLocaleString()} of ${Number(limit).toLocaleString()} bytes (${percent}%)`}
      >
        <div
          role="meter"
          aria-label={label}
          aria-valuemin={0}
          aria-valuemax={limit}
          aria-valuenow={Math.min(value, limit)}
          aria-valuetext={`${display} of ${limitDisplay}, ${word}`}
          className="pgop-meter"
          style={{ background: `var(--pgop-tone-${tone}-bg)` }}
        >
          <div
            className="pgop-meter-fill"
            style={{ width: `${Math.min(ratio, 1) * 100}%`, background: `var(--pgop-tone-${tone}-fg)` }}
          />
        </div>
      </Tooltip>
    </Stack>
  )
}
