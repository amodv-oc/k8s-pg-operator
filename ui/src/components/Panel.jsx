import { Card, Group, Text, Title } from '@mantine/core'

/**
 * A titled section. Used everywhere so every block has the same chrome.
 *
 * `bleed` drops the body's padding so a table can span the container edge to
 * edge, which is what keeps a nine-column list readable: the header strip and
 * the rows then share one set of column edges instead of the rows being inset
 * from the strip above them.
 */
export function Panel({ title, description, actions, bleed, children, ...props }) {
  const hasHeader = Boolean(title || actions || description)

  return (
    <Card padding={0} {...props}>
      {hasHeader && (
        <Card.Section
          p="lg"
          withBorder
          // The header keeps the container's own line under it; rows inside the
          // body separate on the lighter hairline instead.
          style={{ borderBottomColor: 'var(--pgop-line)' }}
        >
          <Group justify="space-between" wrap="wrap" align="flex-end" gap="md">
            {(title || description) && (
              <div>
                {title && (
                  <Title order={4} className="pgop-heading">
                    {title}
                  </Title>
                )}
                {description && (
                  <Text
                    c="dimmed"
                    fz="sm"
                    mt={5}
                    className="pgop-wrap"
                    style={{ textWrap: 'pretty' }}
                  >
                    {description}
                  </Text>
                )}
              </div>
            )}
            {actions}
          </Group>
        </Card.Section>
      )}
      <Card.Section p={bleed ? 0 : 'lg'}>{children}</Card.Section>
    </Card>
  )
}
