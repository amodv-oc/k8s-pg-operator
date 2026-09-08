import { Alert, Code, Stack, Text } from '@mantine/core'
import { IconAlertTriangle, IconPlugConnectedX } from '@tabler/icons-react'

/**
 * The API's failure modes are few and each says something specific, so they
 * are named rather than collapsed into "something went wrong".
 */
function describe(error) {
  const status = error?.status
  if (status === 0) {
    return {
      title: 'The state API is not answering',
      icon: <IconPlugConnectedX size={18} />,
      hint: 'The dashboard was served, so nginx is up. The api container in the same pod is not.',
    }
  }
  if (status === 503) {
    return {
      title: 'The operator cannot reach Kubernetes',
      icon: <IconPlugConnectedX size={18} />,
      hint: 'The API is running but its client to the cluster is failing. It reports the reason below.',
    }
  }
  if (status === 404) {
    return { title: 'Not found', icon: <IconAlertTriangle size={18} />, hint: null }
  }
  if (status === 502) {
    return {
      title: 'The operator reported an error',
      icon: <IconAlertTriangle size={18} />,
      hint: null,
    }
  }
  return {
    title: status ? `Request failed with ${status}` : 'Request failed',
    icon: <IconAlertTriangle size={18} />,
    hint: null,
  }
}

export function ErrorState({ error }) {
  const { title, icon, hint } = describe(error)
  return (
    <Alert color="red" icon={icon} title={title}>
      <Stack gap="xs">
        {error?.detail && <Code block>{error.detail}</Code>}
        {hint && (
          <Text fz="xs" c="dimmed">
            {hint}
          </Text>
        )}
      </Stack>
    </Alert>
  )
}
