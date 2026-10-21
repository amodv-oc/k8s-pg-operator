import { ErrorState } from '../components/ErrorState'
import { FilterBar } from '../components/FilterBar'
import { NoRows } from '../components/NoRows'
import { Page } from '../components/Page'
import { PageHeader } from '../components/PageHeader'
import { Panel } from '../components/Panel'
import { ReplicationsTable } from '../components/ReplicationsTable'
import { TableSkeleton } from '../components/TableSkeleton'
import { useFilters } from '../lib/filters'
import { COUNTS, REPLICATION_STATES } from '../lib/phase'
import { useReplications } from '../lib/queries'

const FIELDS = ['namespace', 'instance', 'database', 'phase', 'state']
const PHASE_OPTIONS = COUNTS.map(({ phase }) => phase)
const STATE_OPTIONS = Object.keys(REPLICATION_STATES)

export function Replications() {
  const filters = useFilters(FIELDS)
  const { data, isPending, isError, error } = useReplications(filters.values)
  const rows = data ?? []

  return (
    <Page>
      <PageHeader
        title="Replications"
        subtitle="One-way logical replication of selected tables from a database on one instance into a database on another."
      />

      <Panel
        bleed={rows.length > 0}
        actions={
          <FilterBar
            filters={filters}
            fields={[
              { name: 'namespace', label: 'Namespace' },
              { name: 'instance', label: 'Instance' },
              { name: 'database', label: 'Database' },
              { name: 'phase', label: 'Phase', type: 'select', options: PHASE_OPTIONS },
              { name: 'state', label: 'State', type: 'select', options: STATE_OPTIONS, width: 150 },
            ]}
            summary={data && `${rows.length} ${rows.length === 1 ? 'replication' : 'replications'}`}
          />
        }
      >
        {isError && <ErrorState error={error} />}
        {isPending && !isError && <TableSkeleton />}
        {data && !rows.length && (
          <NoRows
            title="No replications"
            description={
              filters.active
                ? 'No replication matches these filters.'
                : 'Create a PostgresReplication between two PostgresDBs on different instances.'
            }
          />
        )}
        {rows.length > 0 && <ReplicationsTable rows={rows} />}
      </Panel>
    </Page>
  )
}
