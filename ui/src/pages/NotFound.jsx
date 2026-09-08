import { Button, EmptyState } from '@mantine/core'
import { IconQuestionMark } from '@tabler/icons-react'
import { Link } from 'react-router-dom'

import { Page } from '../components/Page'

export function NotFound() {
  return (
    <Page>
      <EmptyState
        mt="xl"
        icon={<IconQuestionMark size={26} />}
        title="No such page"
        description="The dashboard has an overview, and a list and detail view for each of the three kinds."
      >
        <EmptyState.Actions>
          <Button component={Link} to="/">
            Back to the overview
          </Button>
        </EmptyState.Actions>
      </EmptyState>
    </Page>
  )
}
