import { Box } from '@mantine/core'

/**
 * The content container.
 *
 * The shell itself is unpadded so the overview's band can run edge to edge, so
 * every page supplies this instead: one measure, one gutter, and enough room
 * at the bottom that the floating refresh button never covers the last row.
 */
export function Page({ children, ...props }) {
  return (
    <Box
      maw={1440}
      mx="auto"
      px={{ base: 'md', sm: 'lg', md: '2xl' }}
      pt={{ base: 'lg', md: '2xl' }}
      pb={96}
      {...props}
    >
      {children}
    </Box>
  )
}
