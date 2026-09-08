# pg-operator

A Kubernetes operator that manages databases, roles and grants on **external**
PostgreSQL servers — typically Amazon RDS. It does not run PostgreSQL. It points
at a server you already have and reconciles objects inside it from three custom
resources.

```
PostgresInstance   a server + the credentials used to manage it   (cluster-scoped)
PostgresDB         a database, its schemas, extensions, roles     (namespaced)
PostgresUser       a login role and the access it holds           (namespaced)
```

## Table of contents

- [How it works](#how-it-works)
- [The access model](#the-access-model)
- [Full reconciliation](#full-reconciliation)
- [Retention](#retention)
- [Credentials](#credentials)
- [Install](#install)
- [Quick start](#quick-start)
- [The state API](#the-state-api)
- [The dashboard](#the-dashboard)
- [Resource reference](#resource-reference)
- [Operating notes](#operating-notes)
- [RDS specifics](#rds-specifics)
- [Development](#development)
- [Design decisions](#design-decisions)

## How it works

The operator watches the three resources and, on every reconcile, makes the
server match the spec. A `PostgresInstance` carries a reference to a Secret
holding superuser credentials — on RDS, the master user. Those credentials are
the only ones the operator uses to manage everything else.

```
                     ┌────────────────────────────────────────────┐
  PostgresInstance ──│  host, port, sslMode, retentionPolicy      │
  (cluster-scoped)   │  credentialsSecretRef ──▶ Secret (any ns)  │
                     └────────────────┬───────────────────────────┘
                                      │ referenced by name
                ┌─────────────────────┴─────────────────────┐
                │                                           │
        PostgresDB (ns: team-a)                    PostgresUser (ns: team-a)
        ├─ database  orders                        ├─ role  orders_api
        ├─ schemas   public, audit                 ├─ access
        └─ group roles                             │   └─ dbRef orders, role RW
           ├─ orders_owner                         ├─ Secret orders-api-pg-credentials
           ├─ orders_rw    ◀───── GRANT ───────────┤
           └─ orders_ro                            └─ PushSecret ──▶ external store
```

The reconciler, a read-only FastAPI state API and a React dashboard run in one
pod as three containers.

## The access model

Each `PostgresDB` is backed by three `NOLOGIN` group roles. A `PostgresUser` is
a `LOGIN` role that is granted **membership** of one of them:

| Level   | Group role     | Privileges |
| ------- | -------------- | ---------- |
| `OWNER` | `<db>_owner`   | Owns the database and every managed schema; full DDL |
| `RW`    | `<db>_rw`      | `SELECT`/`INSERT`/`UPDATE`/`DELETE`/`TRUNCATE`/`REFERENCES` on tables, `USAGE`/`SELECT`/`UPDATE` on sequences, `EXECUTE` on routines, `TEMPORARY` on the database |
| `RO`    | `<db>_ro`      | `SELECT` on tables and sequences, `EXECUTE` on routines. No `TEMPORARY` |

A user never holds object privileges directly. Three consequences follow, and
they are the reason for the design:

- **Adding a schema needs no per-user work.** Grant the new schema to the three
  group roles once and every existing member has it.
- **Changing a level is two statements.** `RW` → `RO` is a `REVOKE` and a
  `GRANT` of membership, with no object-level churn.
- **Privileges are auditable in one place.** `status.schemaGrants` reports what
  each group role actually holds, read back from the server.

Access is declared **on the user**, not the database:

```yaml
kind: PostgresDB          # infrastructure: what exists
spec:
  schemas: [{name: public}, {name: audit}]

kind: PostgresUser        # access: who may reach it
spec:
  access:
    - dbRef: {name: orders}
      role: RW
```

One owner per fact means no conflict resolution, and a user's total access is
visible on one object.

### Why owner-group members get `SET role`

A table's default privileges are keyed on the role that **created** it. If a
migration job logs in as `orders_migrator` and runs `CREATE TABLE`, the table is
owned by `orders_migrator`, the `orders_owner` group's `ALTER DEFAULT
PRIVILEGES` never fire, and `orders_rw` cannot see the new table. This is the
single most common way a group-role setup breaks.

The operator prevents it: for each `OWNER` grant it runs

```sql
ALTER ROLE orders_migrator IN DATABASE orders SET role TO orders_owner;
```

so objects that role creates belong to the group. Disable with
`spec.setRoleForOwners: false` on the `PostgresDB`, in which case default
privileges are also applied for the individual login roles.

## Full reconciliation

Every reconcile is a complete convergence pass, not a create-once step. The
create, update, resume and periodic-timer handlers all run the same code.

Adding a schema to an existing `PostgresDB`:

```yaml
spec:
  schemas:
    - name: public
    - name: audit
    - name: reporting     # ← added later
```

On the next pass the operator creates `reporting`, sets its owner, revokes
`CREATE` from `PUBLIC`, grants `USAGE` to all three group roles, grants on
existing objects, and sets default privileges for future ones. Every user in
`orders_rw` can use it immediately. The same applies to extensions, database
parameters, comments, connection limits and ownership.

Reconcile runs every `reconcile.interval` seconds (default 300) as well as on
change, so drift applied directly with `psql` is corrected. Steady state is a
genuine no-op: no statements, no status churn.

Fields PostgreSQL cannot change after creation — `encoding`, `lcCollate`,
`lcCtype` — are reported as drift rather than silently ignored:

```
Synced   False   ImmutableFieldDrift   encoding is UTF8, spec requests LATIN1
                                       (fixed at creation; requires a dump and
                                       reload to change)
```

## Retention

`retentionPolicy` decides what happens to **real data**. It defaults to
`RETAIN`, and a child inherits its instance's policy when it sets none.

| | `RETAIN` (default) | `DROP` |
| --- | --- | --- |
| `PostgresDB` deleted | database and group roles kept | sessions terminated, database dropped, group roles dropped |
| Schema removed from spec | kept, reported in `status.orphanedSchemas`, `Drifted` condition | `DROP SCHEMA ... CASCADE` |
| Extension removed from spec | kept and reported | `DROP EXTENSION ... CASCADE` |
| `PostgresUser` deleted | role and credentials Secret kept | sessions terminated, privileges cleared, role dropped, Secret and PushSecret removed |
| `PostgresInstance` deleted | connections closed; **the server is never touched** | same — the operator does not own the server |

Two safeguards apply regardless of policy:

- **Only objects the operator created are eligible.** What it manages is
  recorded in `status.managedSchemas` / `status.managedExtensions`. A schema a
  DBA created appears in `status.unmanagedSchemas` and is never touched, even
  under `DROP`.
- **A `CASCADE` drop is never silent.** It is logged at warning level and noted
  in the status change list, saying whether the schema held objects.

Retained orphans surface as a condition rather than being quietly kept:

```
$ kubectl get postgresdb orders
NAME     INSTANCE   DATABASE   RETENTION   SCHEMAS   EXTENSIONS   PHASE     AGE
orders   prod-rds   orders     RETAIN      2         1            Drifted   9d

Drifted   True   RetainedOrphans   schema(s) audit no longer declared;
                                   retentionPolicy is RETAIN so nothing was dropped
```

## Credentials

The operator generates a 32-character password (configurable, 16–128) from a
URL- and shell-safe alphabet and writes it to a Secret in the consuming
namespace:

```
orders-api-pg-credentials
  username  orders_api
  password  ································
  host      prod.abc123.ap-southeast-2.rds.amazonaws.com
  port      5432
  database  orders
  sslmode   verify-full
  uri       postgresql://orders_api:...@prod...:5432/orders?sslmode=verify-full
  jdbcUri   jdbc:postgresql://prod...:5432/orders?user=...&password=...
```

Key names are renameable via `generatedSecret.keys` (e.g. `password:
PGPASSWORD`). Values are percent-encoded in the URIs, so a password containing
`/` or `?` cannot corrupt the connection string.

**Bring your own** with `spec.passwordSecretRef`: the operator reads that Secret
and applies the value with `ALTER ROLE`. It never writes to it.

**No credential write in steady state.** The password is generated once and
then re-read from the Secret the operator wrote, so a reconcile of an unchanged
user issues no `ALTER ROLE ... PASSWORD`.

**Self-healing, not rotation.** If the credentials Secret is deleted the old
password is unrecoverable, so the operator generates a new one, applies it, and
republishes. Scheduled rotation is deliberately not in this iteration; when it
lands it will be an explicit, auditable trigger.

### PushSecret

With `spec.pushSecret` (or an instance-wide default), the operator declares an
`external-secrets.io` `PushSecret` alongside the Secret, and external-secrets
mirrors it to AWS Secrets Manager, Vault, or any other supported backend. The
operator never talks to those backends itself, so no provider credentials or
provider logic live here.

```yaml
pushSecret:
  secretStoreRefs:
    - name: aws-secretsmanager
      kind: ClusterSecretStore
  remoteRefKey: rds/prod/{{ .namespace }}/{{ .name }}
  keys: [username, password, host, port, database]
  deletionPolicy: None
```

`deletionPolicy: None` (the default) matches `RETAIN`: removing the resource
does not destroy the remote value. The API version is a chart value
(`pushSecret.apiVersion`), so targeting `external-secrets.io/v1alpha1` on an
older external-secrets is a values change, not a code change. If the PushSecret
CRD is missing, the instance says so in one condition instead of failing every
user reconcile. A PushSecret that external-secrets cannot sync is surfaced on
the user's status.

## Install

Requires Kubernetes ≥ 1.25 (for CEL validation rules).

```bash
helm install pg-operator ./charts/pg-operator \
  --namespace pg-operator --create-namespace
```

The CRDs are in `charts/pg-operator/crds/`, so Helm installs them but does not
upgrade or delete them. Apply CRD changes explicitly:

```bash
kubectl apply -f charts/pg-operator/crds/
```

Values worth reviewing:

| Value | Default | Notes |
| --- | --- | --- |
| `image.repository` | `ghcr.io/your-org/pg-operator` | Set this |
| `watchNamespaces` | `[]` (all) | `PostgresInstance` is always watched cluster-wide |
| `reconcile.interval` | `300` | Full convergence pass, in seconds |
| `pushSecret.apiVersion` | `external-secrets.io/v1` | Use `v1alpha1` below external-secrets 0.14 |
| `rbac.namespacedSecrets` | `false` | See below |
| `api.enabled` | `true` | The state API container |
| `replicaCount` / `peering.enabled` | `1` / `false` | Raise replicas only with peering on |
| `networkPolicy.egress` | `[]` (allow all) | Pin to your RDS CIDRs |

### RBAC and cross-namespace Secrets

The operator reads instance credentials from wherever they live and writes
generated credentials into consuming namespaces, so it holds cluster-wide
Secret access by default. Kubernetes cannot narrow that to a label selector. If
the namespaces are known and fixed, use `Role`s instead:

```yaml
rbac:
  namespacedSecrets: true
  secretNamespaces: [pg-operator, team-a, team-b]
```

`rbac.createViewerRole` (default on) aggregates the CRDs into the built-in
`view` and `edit` cluster roles, so existing RBAC covers them.

## Quick start

```bash
# 1. Superuser credentials. On RDS this is the master user.
kubectl -n pg-operator create secret generic prod-rds-superuser \
  --from-literal=username=postgres \
  --from-literal=password='...'

# 2. Register the server (cluster-scoped).
kubectl apply -f - <<'YAML'
apiVersion: postgres.ourcommunity.com.au/v1alpha1
kind: PostgresInstance
metadata:
  name: prod-rds
spec:
  host: prod.abc123.ap-southeast-2.rds.amazonaws.com
  credentialsSecretRef:
    name: prod-rds-superuser
    namespace: pg-operator
  sslMode: require
YAML

kubectl get postgresinstance prod-rds
# NAME       HOST                  VERSION   RETENTION   DBS   USERS   PHASE   AGE
# prod-rds   prod.abc123...        16.3      RETAIN      0     0       Ready   5s

# 3. A database and a user.
kubectl apply -f - <<'YAML'
apiVersion: postgres.ourcommunity.com.au/v1alpha1
kind: PostgresDB
metadata: {name: orders, namespace: team-a}
spec:
  instanceRef: {name: prod-rds}
  schemas: [{name: public}, {name: audit}]
---
apiVersion: postgres.ourcommunity.com.au/v1alpha1
kind: PostgresUser
metadata: {name: orders-api, namespace: team-a}
spec:
  instanceRef: {name: prod-rds}
  access: [{dbRef: {name: orders}, role: RW}]
YAML

# 4. Use the credentials.
kubectl -n team-a get secret orders-api-pg-credentials \
  -o jsonpath='{.data.uri}' | base64 -d
```

Applying a `PostgresUser` and its `PostgresDB` together is fine: a missing
reference is a temporary error and retries until the database reconciles.

See [`examples/`](examples/) for instances with TLS verification, role-name
overrides, BYO passwords, expiring accounts and PushSecret configurations. Every
example is validated against the models in CI.

## The state API

A read-only FastAPI service runs as a second container in the operator pod. It
projects what the reconciler wrote to resource status, so it is stateless and
can be scaled out later for a frontend.

```bash
kubectl -n pg-operator port-forward svc/pg-operator-api 8000:8000
open http://localhost:8000/docs
```

| Endpoint | Returns |
| --- | --- |
| `GET /healthz` | Liveness. Never touches Kubernetes |
| `GET /readyz` | Ready only when the API server answers and the CRDs exist |
| `GET /api/v1/overview` | Phase counts across all kinds, plus what needs attention |
| `GET /api/v1/instances` | `?phase=` |
| `GET /api/v1/instances/{name}` | Instance plus its databases and users |
| `GET /api/v1/databases` | `?namespace=` `?instance=` `?phase=` `?drifted=` |
| `GET /api/v1/databases/{ns}/{name}` | Database plus the users granted on it |
| `GET /api/v1/users` | `?namespace=` `?instance=` `?database=` `?access=` `?phase=` |
| `GET /api/v1/users/{ns}/{name}` | One user |

```console
$ curl -s localhost:8000/api/v1/overview | jq
{
  "instances": {"ready": 2, "unreachable": 0, "total": 2},
  "databases": {"ready": 11, "drifted": 1, "total": 12},
  "users":     {"ready": 26, "failed": 0, "total": 26},
  "healthy": false,
  "attention": [
    "PostgresDB/team-a/reporting retains orphaned object(s): legacy"
  ],
  "generated_at": "2026-09-07T04:15:22Z"
}
```

**Credential values are never returned.** A user response names the Secret
holding its credentials and lists the keys inside it. A test asserts that no
endpoint emits a password or URI field.

Response models are a deliberate projection rather than raw custom resources, so
the shape stays stable as the CRDs evolve. `api.corsOrigins` allows a browser
frontend on another origin; `api.ingress` exposes the API on its own.

## The dashboard

A React dashboard over the same API runs as a third container in the operator
pod. nginx serves the bundle and proxies the API paths to `127.0.0.1:8000`:

```
pod pg-operator
├── operator   kopf      :8080 health   :9090 metrics
├── api        uvicorn   :8000
└── ui         nginx     :8081
                 /               the bundle, with an index.html fallback
                 /api/v1/*   ┐
                 /readyz     ├── proxy_pass → 127.0.0.1:8000
                 /healthz    │
                 /docs       ┘
                 /nginx-healthz  nginx's own 200, for this container's probe
```

Because the proxy target is loopback inside the same pod the browser is
**same-origin**: no CORS, no cluster DNS lookup, no NetworkPolicy egress rule,
and no service-account token mounted for the UI. One ingress — or one
port-forward — reaches the dashboard *and* `/docs`.

```bash
kubectl -n pg-operator port-forward svc/pg-operator-api 8081:8081
open http://localhost:8081/
```

| View | Shows |
| --- | --- |
| Overview | Phase counts per kind, each a link into the matching filtered list, plus the `attention` list rendered as links |
| Instances | Endpoint, server version, managing role and its privileges, SSL mode, retention; then every database and user on it |
| Databases | Group roles, and **one row per schema** tagged `managed` / `orphaned` / `unmanaged` / `unowned` / `missing` with the grants each group role actually holds; extensions, denied parameters, and the users granted on it |
| Users | Grants as *database → level → group role*, the credentials Secret **name and keys only**, PushSecret ref, and whether the password is generated or supplied |

Every view shows the raw conditions, so a `Drifted` resource names its reason —
`RetainedOrphans`, `SchemaNotOwned`, `ParameterDenied`, `ImmutableFieldDrift` or
`MultipleIssues` — rather than just its colour. Filters live in the URL and map
one-to-one onto the API's query parameters, so a filtered view is a shareable
link. It polls every 10s by default (`ui.config.refreshIntervalMs`), which the
API's own 5s response cache absorbs.

The UI is plain JavaScript — no TypeScript — on [Mantine](https://mantine.dev)
9, themed from Tailwind's design tokens: its palettes folded into Mantine's
ten-slot colour arrays, its spacing, type, radius and shadow scales, and its
`font-sans` stack. No Tailwind dependency, and no webfont, so the page makes no
external request at all.

**It inherits the API's exposure model.** Read-only, and it never returns a
credential value — but it does enumerate database, role and Secret names, and
neither it nor the API authenticates. Gate it at the ingress.

## Resource reference

### PostgresInstance (cluster-scoped)

| Field | Default | Notes |
| --- | --- | --- |
| `host`, `port` | — / `5432` | The server endpoint |
| `credentialsSecretRef` | — | `name`, `namespace`, `usernameKey`, `passwordKey` |
| `maintenanceDatabase` | `postgres` | Used for cluster-wide work; never modified |
| `sslMode` | `require` | `verify-ca`/`verify-full` require `sslRootCertSecretRef` |
| `sslRootCertSecretRef` | — | CA bundle, written to disk for libpq |
| `retentionPolicy` | `RETAIN` | Inherited by children that set none |
| `rolePrefix` | `""` | Namespaces generated role names per environment |
| `allowedNamespaces` | `[]` (all) | Which namespaces may reference this instance |
| `minServerVersion` | — | Refuse to act below this major version |
| `pushSecret` | — | Instance-wide default |
| `paused` | `false` | Suspend reconciliation |

### PostgresDB (namespaced)

| Field | Default | Notes |
| --- | --- | --- |
| `instanceRef.name` | — | Required |
| `databaseName` | sanitised `metadata.name` | Immutable |
| `retentionPolicy` | inherits instance | `RETAIN` or `DROP` |
| `schemas[]` | `[public]` | `name`, `comment`. `public` always managed |
| `extensions[]` | `[]` | `name`, `schema`, `version`, `cascade` |
| `roles` | derived | Override `owner`/`readWrite`/`readOnly` names |
| `encoding`, `lcCollate`, `lcCtype`, `template` | `UTF8`, —, —, `template0` | Creation-only |
| `connectionLimit` | `-1` | Unlimited |
| `parameters` | `{}` | `ALTER DATABASE ... SET`; removals are `RESET` |
| `revokePublicConnect` | `true` | Only the group roles may attach |
| `revokePublicSchemaCreate` | `true` | No-op on PG ≥ 15 for `public` |
| `grantExecuteToReadOnly` | `true` | `PUBLIC` holds `EXECUTE` by default anyway |
| `setRoleForOwners` | `true` | See [above](#why-owner-group-members-get-set-role) |

### PostgresUser (namespaced)

| Field | Default | Notes |
| --- | --- | --- |
| `instanceRef.name` | — | Required |
| `username` | sanitised `metadata.name` | Immutable; `pg_` prefix rejected |
| `access[]` | `[]` | `dbRef` (`name`, `namespace`) and `role`: `OWNER`/`RW`/`RO` |
| `retentionPolicy` | inherits instance | |
| `attributes` | all off, `inherit: true` | `createDB`, `createRole`, `replication`, `bypassRLS` |
| `connectionLimit`, `validUntil` | `-1`, — | |
| `parameters` | `{}` | `ALTER ROLE ... SET`; removals are `RESET` |
| `login` | `true` | `false` makes a group-only role |
| `passwordSecretRef` | — | BYO password; read, never written |
| `passwordLength` | `32` | 16–128 |
| `generatedSecret` | — | `name`, `namespace`, `keys`, `labels`, `annotations`, `includeUri`, `defaultDatabase` |
| `pushSecret` | inherits instance | Overrides the instance default |

## Operating notes

### Status and conditions

```bash
kubectl get postgresinstances
kubectl get postgresdbs -A
kubectl get postgresusers -A
kubectl describe postgresdb orders -n team-a
```

Conditions follow `metav1.Condition`, with `observedGeneration` so a stale one
is recognisable. `lastTransitionTime` only moves when the status actually flips.

| Condition | Meaning |
| --- | --- |
| `Ready` | Last reconcile succeeded |
| `Reachable` | The server answered |
| `Synced` | Spec fully applied. `False` when something in the spec cannot be satisfied |
| `Drifted` | Live state diverges from the spec and this pass did not converge it |

`status.phase` collapses these into one printer column: `Ready`, `Drifted`,
`Unreachable`, `Failed`, `Paused`, `Pending`.

`Drifted` covers two different situations, and its `reason` says which:

| Reason | Divergence the operator... | Clears `Synced`? |
| --- | --- | --- |
| `RetainedOrphans` | *will not* fix — `retentionPolicy: RETAIN` forbids dropping | No |
| `SchemaNotOwned` | *cannot* fix — the managing role cannot take the schema | Yes |
| `ParameterDenied` | *cannot* fix — the managing role may not set the parameter | Yes |
| `ImmutableFieldDrift` | *cannot* fix — PostgreSQL has no `ALTER` for the field | Yes |
| `MultipleIssues` | more than one of the above; every cause is in the message | Yes |

A cause that the operator cannot fix does not abort the pass. Schemas,
extensions, grants and roles still converge, and the specific thing that failed
is named on `status.unownedSchemas` or `status.deniedParameters` with a remedy.
A single unsettable parameter blocking every other object in the database would
be a worse failure than the one being reported.

### Failure behaviour

A misconfigured spec is a **permanent** error — retrying cannot help until the
spec changes. A missing reference, an unreachable server or a failed statement
is **temporary** and retried with backoff. Failures are written to status, so
the reason is on the object rather than only in the logs.

### Metrics

Prometheus metrics on `:9090/metrics`, with a `ServiceMonitor` available:

```
pg_operator_reconcile_total{kind,outcome}
pg_operator_reconcile_duration_seconds{kind}
pg_operator_resource_ready{kind,namespace,name}
pg_operator_teardown_total{kind,outcome}
```

### Pausing

`spec.paused: true` on any resource suspends reconciliation before any
connection is opened — useful during a manual migration, or to stop the operator
fighting a human. The resource reports `Paused`.

### Replicas

Run one replica. The reconciler issues DDL, and two active operators would
conflict. The Deployment uses the `Recreate` strategy for the same reason. To
run more, enable `peering.enabled=true` so kopf elects a single active instance.

### A note on silent grant failures

PostgreSQL emits a **WARNING**, not an error, when `GRANT` or `REVOKE` is issued
by a role lacking authority — so an under-privileged operator would produce a
database with no grants and no visible failure. Every connection installs a
notice handler that logs server warnings, and `status.schemaGrants` reports
privileges read back from the server rather than what was requested.

## RDS specifics

The RDS master user is **not** a superuser. It is a member of `rds_superuser`
with `CREATEDB` and `CREATEROLE`. The operator is built and tested against
exactly that constraint, which changes several things:

- **Role ownership needs explicit grants.** From PostgreSQL 16, a `CREATEROLE`
  role that creates a role receives `ADMIN OPTION` but `INHERIT FALSE, SET
  FALSE`. Naming a group role as an owner needs the `SET` option; then *acting*
  as that owner — `CREATE SCHEMA`, `ALTER DEFAULT PRIVILEGES`, `CREATE
  EXTENSION` — needs `INHERIT`. The operator grants itself both.
- **Attributes are only altered when they change.** PostgreSQL refuses `ALTER
  ROLE ... NOREPLICATION` from a role without `REPLICATION`, *even when the
  target already has `NOREPLICATION`*. The master user has neither
  `REPLICATION` nor `BYPASSRLS`, so restating current values would fail every
  reconcile. Only genuine differences are emitted.
- **`DROP OWNED BY` needs the privileges of the role being cleared**, which
  creating it does not confer. The operator grants itself membership first.
- **`DROP DATABASE ... WITH (FORCE)`** needs the right to signal other users'
  backends. The operator closes the database to new connections, terminates
  sessions, and falls back to a plain drop if `FORCE` is refused — restoring
  connections if the drop cannot complete.
- **`pg_authid` is unreadable**, so a password cannot be verified. The check
  abstains rather than guessing, which is what stops a reconcile from rewriting
  a working credential it cannot read.
- **Extensions must be allowed.** An extension needs to be permitted by
  `rds.allowed_extensions` and, where required, preloaded via
  `shared_preload_libraries`. An unavailable extension is reported as drift with
  that hint, not retried forever.

TLS: use `sslMode: verify-full` with the AWS CA bundle for real server
authentication.

```bash
curl -o rds-ca.pem https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem
kubectl -n pg-operator create secret generic rds-ca-bundle --from-file=ca.pem=rds-ca.pem
```

## Development

```bash
uv sync --extra dev

uv run ruff check src tests        # lint
uv run mypy src/pg_operator        # types
uv run pytest                      # unit tests
helm lint charts/pg-operator       # chart

make ui-install                    # the dashboard's npm dependencies
make ui-check                      # ESLint and a production build
```

`make check` runs all of it.

### Integration tests

The integration suite runs against a real PostgreSQL server as a **non-superuser**,
mirroring RDS. It is where the privilege model is actually proved — and it is
how most of the RDS-specific bugs in this operator were found. It is verified
against PostgreSQL 14, 15, 16, 17 and 18, which take different code paths:
per-grant `INHERIT`/`SET` options arrive in 16, `pg_database_owner` ownership of
`public` in 15, and `GRANT ... ON PARAMETER` in 15.

```bash
docker run -d --name pgop-test -e POSTGRES_PASSWORD=testpw -p 15432:5432 postgres:16-alpine

# An RDS-like managing role: member of rds_superuser, not a superuser.
docker exec pgop-test psql -U postgres -c "CREATE ROLE rds_superuser NOLOGIN"
docker exec pgop-test psql -U postgres -c "CREATE ROLE pgadmin LOGIN PASSWORD 'adminpw' CREATEDB CREATEROLE"
docker exec pgop-test psql -U postgres -c "GRANT rds_superuser TO pgadmin"

export PGOP_TEST_DSN="host=localhost port=15432 user=pgadmin password=adminpw dbname=postgres sslmode=disable"
uv run pytest
```

Without `PGOP_TEST_DSN` the integration tests skip and the unit tests still run.
`tests/integration/test_reconcile_end_to_end.py` drives the real reconcilers
against real PostgreSQL with a faked Kubernetes API, so it covers ordering,
status and retention decisions rather than SQL primitives alone.

### The local cluster harness

`hack/local/` runs the operator in a local Kubernetes cluster against five real
servers — PostgreSQL 14 through 18 — each bootstrapped with an RDS-like
non-superuser managing role. Tested on OrbStack, whose image store is shared
with Docker so no registry is needed.

```bash
make local-up        # build both images, start the servers, install the operator
make local-status    # phases for all three kinds
make local-verify    # connect as each generated user and assert its privileges
make local-test      # the full pytest suite against each server in turn
make local-ui        # port-forward the dashboard to localhost:8081
make local-down
```

Applying an identical workload to every version is the point: a behavioural
difference between 14 and 18 shows up as a status difference between otherwise
identical resources. PostgreSQL 14 is *expected* to report `Drifted` there — see
`hack/local/README.md` for why, and why a real RDS 14 instance does not.

### Working on the dashboard

Vite proxies the same API prefixes nginx does, so the dev server behaves like
the deployed pod — same-origin, no CORS:

```bash
make local-api    # in one shell: port-forward the API to localhost:8000
make ui-dev       # in another: http://localhost:5173, hot reloading
```

`make ui-image-check` builds the image and runs it under exactly the
constraints the pod imposes — `--read-only --user 65532:65532` — then asserts
nginx came up, a deep link falls back to `index.html`, a missing asset is still
a 404, and `/api` is proxied. That combination is the one thing about this
container that cannot fail anywhere else.

### Running against a kubecontext directly

```bash
export PG_OPERATOR_NAMESPACE=pg-operator
export PG_OPERATOR_LOG_FORMAT=console
uv run kopf run --module pg_operator.operator --all-namespaces --standalone

uv run pg-operator-api      # the state API, separately
```

### Layout

```
src/pg_operator/
  models.py          typed CR specs; defaults and derivations live here
  naming.py          Kubernetes name → PostgreSQL identifier
  status.py          metav1.Condition helpers
  postgres/
    connection.py    fingerprint-keyed pools; a rotated Secret retires the pool
    sql.py           composition helpers; nothing is string-interpolated
    server.py        version and effective-privilege detection
    roles.py         group roles, login roles, membership, teardown
    database.py      databases, schemas, extensions, parameters
    privileges.py    the OWNER/RW/RO grant matrix
    settings.py      pg_db_role_setting parsing
  k8s/               client, cross-namespace Secrets, PushSecret, CR access
  reconcile/         instance, database and user convergence + teardown
  handlers/          kopf registration, retry mapping, metrics
  api/               FastAPI state API
```

### Building the image

```bash
docker build -t pg-operator:dev .
```

Multi-stage, `python:3.14-slim`, uv-installed, non-root (65532), read-only root
filesystem. `psycopg[binary]` bundles libpq, so no PostgreSQL client packages
are needed.

## Design decisions

Choices worth knowing about, and why:

- **Group roles per database, not direct grants.** A privilege fix applies to
  every member at once, and a new schema needs no per-user re-granting.
- **Access declared on the user only.** One owner per fact; no merge or conflict
  resolution; a user's total access is on one object.
- **`PostgresInstance` is cluster-scoped.** It matches the reality of one shared
  RDS instance with many tenants: the platform team owns instances, application
  teams own databases and users.
- **The API reads from resource status, not from PostgreSQL.** It stays
  stateless and independently restartable, and cannot add load to the database.
- **Managed objects are tracked in status.** Orphan detection compares the spec
  against what the operator created, so nothing it did not create can be
  dropped.
- **Generated Secrets carry no owner reference.** Owner references cannot cross
  namespaces, and `RETAIN` must leave a working credential behind for a role
  that still exists — so deletion is driven by policy, explicitly.
- **Identifiers never reach SQL as text.** Every identifier goes through
  `psycopg.sql.Identifier` and every value through a bound parameter or
  `sql.Literal`. Names are validated at admission by CRD patterns and again in
  the models.
- **No password rotation in this iteration.** Deliberate: rotation under a
  running application needs an explicit, auditable trigger, which is the next
  increment rather than a default.

## License

Apache-2.0
