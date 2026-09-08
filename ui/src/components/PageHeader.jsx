import { Anchor, Breadcrumbs, Group, Stack, Text, Title } from '@mantine/core'
import { Link } from 'react-router-dom'

export function PageHeader({ title, subtitle, crumbs, actions, badge, mono }) {
  return (
    <Stack gap="xs" mb="lg">
      {crumbs?.length > 0 && (
        <Breadcrumbs separator="/" fz="sm">
          {crumbs.map((crumb) =>
            crumb.to ? (
              <Anchor key={crumb.label} component={Link} to={crumb.to} c="dimmed" fz="sm">
                {crumb.label}
              </Anchor>
            ) : (
              <Text key={crumb.label} c="dimmed" fz="sm">
                {crumb.label}
              </Text>
            ),
          )}
        </Breadcrumbs>
      )}
      <Group justify="space-between" align="flex-start" wrap="wrap" gap="md">
        <div>
          <Group gap="sm" align="center" wrap="wrap">
            {/* A detail page is titled with the resource's own name, which is
                an identifier the operator read off a server - so it is set in
                the mono face the rest of the console reads identifiers in. */}
            <Title order={2} className="pgop-heading" ff={mono ? 'monospace' : undefined}>
              {title}
            </Title>
            {badge}
          </Group>
          {subtitle && (
            <Text c="dimmed" fz="md" mt={8} className="pgop-wrap" style={{ textWrap: 'pretty', maxWidth: '70ch' }}>
              {subtitle}
            </Text>
          )}
        </div>
        {actions}
      </Group>
    </Stack>
  )
}
