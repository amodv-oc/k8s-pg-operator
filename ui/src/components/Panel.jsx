import { Card, Group, Text, Title } from '@mantine/core'

/** A titled section. Used everywhere so every block has the same chrome. */
export function Panel({ title, description, actions, children, ...props }) {
  return (
    <Card {...props}>
      {(title || actions) && (
        <Card.Section inheritPadding pb="sm" mb="md" withBorder>
          <Group justify="space-between" wrap="nowrap" align="flex-start">
            <div>
              <Title order={5}>{title}</Title>
              {description && (
                <Text c="dimmed" fz="xs" mt={2}>
                  {description}
                </Text>
              )}
            </div>
            {actions}
          </Group>
        </Card.Section>
      )}
      {children}
    </Card>
  )
}
