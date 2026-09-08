# Local cluster harness

Runs the operator against five real PostgreSQL servers (14, 15, 16, 17, 18) on a
local Kubernetes cluster. Tested on OrbStack, which shares its image store with
Docker so a locally built image needs no registry.

Each server is bootstrapped to look like RDS rather than like a stock install:
the managing role `pgadmin` is a member of `rds_superuser` with `CREATEDB` and
`CREATEROLE`, but is **not** a superuser and holds neither `REPLICATION` nor
`BYPASSRLS`. That is the single most valuable property of this harness — most of
the operator's hard-won behaviour exists because a non-superuser managing role
is refused things a superuser never notices.

Storage is ephemeral. These servers exist to be reconciled against.

## Run it

```sh
make local-up        # build both images, start the servers, install the operator
make local-status    # phases for all three kinds
make local-verify    # functional OWNER/RW/RO checks on all five versions
make local-ui        # port-forward the dashboard to localhost:8081
make local-down      # remove everything
```

`make local-test` runs the whole pytest suite against each server in turn, by
port-forwarding it to `localhost:15432`.

The pod runs three containers — `operator`, `api` and `ui`. One port-forward
reaches the dashboard *and* the API's `/docs`, because the dashboard's nginx
proxies the API on loopback:

```sh
make local-ui        # http://localhost:8081/  and  http://localhost:8081/docs
```

`ui.config.clusterLabel` is set to `orbstack` here so a tab on this cluster is
distinguishable from one on a real cluster, and the poll interval is dropped to
5s to match the shortened reconcile interval.

## What each file does

| File | Contents |
|---|---|
| `00-servers.yaml` | The five servers, plus the RDS-like bootstrap SQL |
| `01-instances.yaml` | Namespaces, credential Secrets, one `PostgresInstance` per server |
| `02-workload.yaml` | The same `PostgresDB` + three `PostgresUser` shapes on every server |
| `verify.sh` | Connects as each generated user and asserts what it may and may not do |

Applying the identical workload to every version is deliberate: any behavioural
difference between 14 and 18 shows up as a status difference between otherwise
identical resources.

## Expected results

Four of the five databases reach `Ready`. **PostgreSQL 14 is expected to be
`Drifted`**, for two reasons that are properties of that server, not faults:

- `public` belongs to the bootstrap superuser before PostgreSQL 15, so a
  non-superuser managing role cannot take its ownership → `SchemaNotOwned`.
- `log_min_duration_statement` is superuser-only, and `GRANT ... ON PARAMETER`
  (the stock way to delegate it) only arrived in PostgreSQL 15 → `ParameterDenied`.
  RDS grants its master user this right on every version, so a real RDS 14
  instance converges cleanly here.

Both are reported with remedies and neither blocks the rest of the database.
`orders-14` is therefore the most useful page in the dashboard: it is the one
resource off the happy path, so it exercises the `Drifted` reason, the
`unowned` schema state and the denied-parameter callout all at once.

### Exercising the retention path

Removing a schema from a spec under `RETAIN` should keep it on the server and
report it. Check the server, not the status — status is written on a reconcile
and cached by the API for a few seconds, so reading it immediately after a
change can show the previous pass:

```sh
kubectl -n team-a patch postgresdb orders-16 --type=json \
  -p='[{"op":"remove","path":"/spec/schemas/2"}]'

kubectl -n pg-instances exec deploy/pg16 -- \
  psql -U pgadmin -d orders -c '\dn'      # reporting is still there

kubectl -n team-a get postgresdb orders-16 -o \
  jsonpath='{.status.orphanedSchemas}{"\n"}'   # ["reporting"]
```

`Drifted` becomes `True` with reason `RetainedOrphans` while `Synced` stays
`True`: a retained orphan is a deliberate outcome, not a failure to converge.
Add the schema back to restore the steady state.

## Notes

- `sslMode: disable` — these servers have no TLS. Do not copy that to a real
  instance; `verify-full` with `sslRootCertSecretRef` is what RDS wants.
- The operator's reconcile interval is set to 60s (default 300s) so drift
  correction is observable within a coffee-length attention span.
- A local VM's clock can drift while the host sleeps. `lastTransitionTime` is
  deliberately frozen at the moment a condition last flipped, so it will show
  the skewed clock long after `lastReconciledAt` has re-synced. Compare the two
  before suspecting a bug.
