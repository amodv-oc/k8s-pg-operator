import { Box, Button, Group, Stack, Text } from '@mantine/core'
import { IconRefresh } from '@tabler/icons-react'
import { useQueryClient } from '@tanstack/react-query'

/**
 * The overview's colour-block moment.
 *
 * The design system runs a deep-green band as the recessed feature zone
 * between cream sections, and this is the console's one use of it: the single
 * sentence that says whether anything needs looking at, set in the serif that
 * appears nowhere else, over the health readout that says whether the answer
 * can be trusted.
 */
export function HeroBand({ headline, eyebrow, subtitle, meta, aside }) {
  const queryClient = useQueryClient()

  return (
    <Box
      className="pgop-band"
      px={{ base: 'md', sm: 'xl', md: '2xl' }}
      py={{ base: 'xl', md: '3xl' }}
    >
      <Group
        align="flex-start"
        justify="space-between"
        wrap="wrap"
        gap="2xl"
        maw={1440}
        mx="auto"
      >
        <Stack gap={0} style={{ flex: '1 1 380px', minWidth: 0 }}>
          <Text className="pgop-eyebrow" c="var(--pgop-gold)">
            {eyebrow}
          </Text>

          <Text
            component="h1"
            className="pgop-display"
            mt={14}
            mb={0}
            fz={{ base: '2xl', md: '3xl' }}
            c="var(--pgop-on-band)"
          >
            {headline}
          </Text>

          {subtitle && (
            <Text fz="lg" lh="lg" mt="md" maw="52ch" c="var(--pgop-on-band-soft)" style={{ textWrap: 'pretty' }}>
              {subtitle}
            </Text>
          )}

          <Group gap="sm" mt="xl" wrap="wrap">
            {/* A filled green pill would vanish into the band, so the primary
                action inverts to white with green text — in both schemes, since
                the band itself is deep green in both. */}
            <Button
              size="md"
              c="var(--pgop-band-cta-fg)"
              bg="var(--pgop-band-cta-bg)"
              leftSection={<IconRefresh size={17} />}
              onClick={() => queryClient.invalidateQueries()}
            >
              Refresh now
            </Button>
            <Button
              component="a"
              href="/docs"
              target="_blank"
              rel="noopener noreferrer"
              size="md"
              variant="outline"
              c="var(--pgop-on-band)"
              style={{ borderColor: 'rgb(255 255 255 / 0.8)' }}
            >
              OpenAPI docs
            </Button>
          </Group>

          {meta && (
            <Text fz="sm" mt="lg" c="var(--pgop-on-band-soft)">
              {meta}
            </Text>
          )}
        </Stack>

        {aside && (
          <Box className="pgop-band-panel" p="lg" style={{ flex: '0 1 360px', minWidth: 280 }}>
            {aside}
          </Box>
        )}
      </Group>
    </Box>
  )
}
