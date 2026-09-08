import { Table, Text } from '@mantine/core'

import { conditionTone } from '../lib/phase'
import { Pill } from './Pill'
import { RelativeTime } from './RelativeTime'

/**
 * The operator's conditions, verbatim.
 *
 * `Drifted=True` takes the gold rather than the red on purpose: the resource did
 * reconcile, and something about the live state diverges anyway. Its `reason`
 * distinguishes the five cases - RetainedOrphans, SchemaNotOwned,
 * ParameterDenied, ImmutableFieldDrift, MultipleIssues.
 */
export function ConditionsTable({ conditions }) {
  if (!conditions?.length) {
    return (
      <Text c="dimmed" fz="sm">
        No conditions reported yet — the resource has not been reconciled.
      </Text>
    )
  }

  return (
    <Table.ScrollContainer minWidth={700} type="native">
      <Table verticalSpacing="xs" fz="sm" highlightOnHover={false}>
        <Table.Thead>
          <Table.Tr>
            <Table.Th w={110}>Type</Table.Th>
            <Table.Th w={90}>Status</Table.Th>
            <Table.Th w={170}>Reason</Table.Th>
            <Table.Th>Message</Table.Th>
            <Table.Th w={150}>Since</Table.Th>
          </Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          {conditions.map((condition) => (
            <Table.Tr key={condition.type}>
              <Table.Td fw={500}>{condition.type}</Table.Td>
              <Table.Td>
                <Pill
                  tone={conditionTone(condition.type, condition.status)}
                  label={condition.status}
                />
              </Table.Td>
              <Table.Td>
                <Text fz="xs" ff="monospace" className="pgop-wrap">
                  {condition.reason || '—'}
                </Text>
              </Table.Td>
              <Table.Td className="pgop-wrap">{condition.message || '—'}</Table.Td>
              <Table.Td>
                <RelativeTime value={condition.lastTransitionTime} fz="xs" c="dimmed" />
              </Table.Td>
            </Table.Tr>
          ))}
        </Table.Tbody>
      </Table>
    </Table.ScrollContainer>
  )
}
