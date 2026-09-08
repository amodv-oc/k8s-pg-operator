import { Anchor, Breadcrumbs, Group, Stack, Text, Title } from '@mantine/core'
import { Link } from 'react-router-dom'

export function PageHeader({ title, subtitle, crumbs, actions, badge }) {
  return (
    <Stack gap="xs" mb="lg">
      {crumbs?.length > 0 && (
        <Breadcrumbs separator="/" fz="xs">
          {crumbs.map((crumb) =>
            crumb.to ? (
              <Anchor key={crumb.label} component={Link} to={crumb.to} c="dimmed" fz="xs">
                {crumb.label}
              </Anchor>
            ) : (
              <Text key={crumb.label} c="dimmed" fz="xs">
                {crumb.label}
              </Text>
            ),
          )}
        </Breadcrumbs>
      )}
      <Group justify="space-between" align="flex-start" wrap="nowrap">
        <div>
          <Group gap="sm" align="center">
            <Title order={3}>{title}</Title>
            {badge}
          </Group>
          {subtitle && (
            <Text c="dimmed" fz="sm" mt={4} className="pgop-wrap">
              {subtitle}
            </Text>
          )}
        </div>
        {actions}
      </Group>
    </Stack>
  )
}
