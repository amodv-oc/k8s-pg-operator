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
make local-up        # build the image, start the servers, install the operator
make local-status    # phases for all three kinds
make local-verify    # functional OWNER/RW/RO checks on all five versions
make local-down      # remove everything
```

`make local-test` runs the whole pytest suite against each server in turn, by
port-forwarding it to `localhost:15432`.

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

## Notes

- `sslMode: disable` — these servers have no TLS. Do not copy that to a real
  instance; `verify-full` with `sslRootCertSecretRef` is what RDS wants.
- The operator's reconcile interval is set to 60s (default 300s) so drift
  correction is observable within a coffee-length attention span.
