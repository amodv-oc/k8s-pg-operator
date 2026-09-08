import { Tooltip } from '@mantine/core'
import { IconRefresh } from '@tabler/icons-react'
import { useIsFetching, useQueryClient } from '@tanstack/react-query'

/**
 * Refresh, as the one persistently elevated control on every page.
 *
 * It floats over whatever is scrolled under it rather than sitting in the
 * header, and spins whenever a request is in flight - which covers the polled
 * refetches as well as a press, so the button is also the console's only
 * indication that it is still talking to the API.
 */
export function RefreshFab() {
  const queryClient = useQueryClient()
  const busy = useIsFetching() > 0

  return (
    <Tooltip label={busy ? 'Refreshing' : 'Refresh now'} position="left">
      <button
        type="button"
        className="pgop-fab"
        aria-label="Refresh now"
        data-busy={busy || undefined}
        onClick={() => queryClient.invalidateQueries()}
      >
        <IconRefresh size={24} />
      </button>
    </Tooltip>
  )
}
