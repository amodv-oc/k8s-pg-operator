# pg-operator

A Kubernetes operator that manages databases, roles and grants on **external**
PostgreSQL servers — typically Amazon RDS. It does not run PostgreSQL. It points
at a server you already have and reconciles objects inside it from four custom
resources.

```
PostgresInstance     a server + the credentials used to manage it    (cluster-scoped)
PostgresDB           a database, its schemas, extensions, roles      (namespaced)
PostgresUser         a login role and the access it holds            (namespaced)
PostgresReplication  selected tables, replicated from a database on  (namespaced)
                     one instance into a database on another
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

The operator watches these resources and, on every reconcile, makes the
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

A `PostgresReplication` joins two `PostgresDB`s on different instances; see
[PostgresReplication](#postgresreplication-namespaced).

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
| `PostgresReplication` deleted | subscription, slots, publication, generated login and Secret removed; replicated rows kept | same — `retentionPolicy` does not apply |

A `RETAIN` teardown never opens a connection, so deleting a retained resource
completes even while its server is unreachable or its credentials Secret is
gone. A `DROP` teardown needs the server and retries until it can reach it.

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
  metadata:
    secretPushFormat: string
    description: managed by pg-operator
    tags:
      env: prod
```

#### One object, one write

All selected keys are pushed as a **single** remote object, via one `dataTo`
entry per store with `remoteKey` set and the keys chosen by an anchored
`match.regexp`. external-secrets bundles them into one JSON document and writes
it in a single call.

The obvious-looking alternative — one `data` entry per key, each with its own
`remoteRef.property` against a shared `remoteKey` — is quietly lossy, and the
operator deliberately does not emit it. Each such entry is an independent
read-modify-write of the *same* remote object (`GetSecretValue`, set one
property, `PutSecretValue`) with no compare-and-swap. Against AWS Secrets
Manager, which is eventually consistent, an entry can read a version predating
the previous entry's write and put back a document missing that key. It also
burns one secret version per key per reconcile, and a single failed entry
aborts every later entry in that pass. Upstream's guidance is the same
([#4746](https://github.com/external-secrets/external-secrets/issues/4746)); the
underlying concurrency gap is [#2662](https://github.com/external-secrets/external-secrets/issues/2662),
open since 2023.

Because `dataTo` is used, `keys` is honoured by pattern rather than by
enumeration — the regexp is anchored (`^(?:username|password)$`) so a key named
`export_port` is not selected by `port`.

`metadata` carries provider-specific push options. The operator wraps it in the
envelope external-secrets expects — `apiVersion:
kubernetes.external-secrets.io/v1alpha1`, `kind: PushSecretMetadata` — and
emits it as the `metadata` of every data entry, but passes the body through
unvalidated: the fields above are the AWS Secrets Manager set, while Vault
takes `customMetadata` and GCP takes `annotations`/`labels`. Only the
external-secrets provider knows which is which, and encoding that here would
put provider logic back into an operator that deliberately has none. Omitting
`metadata` omits the envelope entirely.

`deletionPolicy: None` (the default) matches `RETAIN`: removing the resource
does not destroy the remote value. The API version is a chart value
(`pushSecret.apiVersion`), so following a future promotion of the kind is a
values change, not a code change. If the PushSecret
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

Both images are published to GitHub Container Registry by CI and are the chart's
defaults, so a plain `helm install` needs no image configuration:

```
ghcr.io/amodv-oc/k8s-pg-operator      the reconciler and the state API
ghcr.io/amodv-oc/k8s-pg-operator-ui   the dashboard
```

Values worth reviewing:

| Value | Default | Notes |
| --- | --- | --- |
| `image.repository` | `ghcr.io/amodv-oc/k8s-pg-operator` | Published by CI; override for your own registry |
| `ui.image.repository` | `ghcr.io/amodv-oc/k8s-pg-operator-ui` | The dashboard image, same pipeline |
| `watchNamespaces` | `[]` (all) | `PostgresInstance` is always watched cluster-wide |
| `reconcile.interval` | `300` | Full convergence pass, in seconds |
| `reconcile.replicationInterval` | `60` | A `PostgresReplication`'s pass, which also observes its health |
| `metrics.prometheusRule.enabled` | `false` | Alerts on replication health, for the Prometheus Operator |
| `pushSecret.apiVersion` | `external-secrets.io/v1alpha1` | The version external-secrets serves `PushSecret` under |
| `rbac.namespacedSecrets` | `false` | See below |
| `api.enabled` | `true` | The state API container |
| `ui.enabled` | `true` | The dashboard container; requires `api.enabled` |
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
overrides, BYO passwords, expiring accounts, PushSecret configurations, and a
replication between two instances (`04-replication.yaml`). Every example is
validated against the models in CI.

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
| `GET /api/v1/replications` | `?namespace=` `?instance=` (either end) `?database=` (either end, as `ns/name` or its database name) `?phase=` `?state=` |
| `GET /api/v1/replications/{ns}/{name}` | One replication, with every table: subscribed and its sync state, or held back and why |

Instance and database detail include the replications from or into them, and
an instance reports its `logical_replication` readiness. A replication names
its login's Secret and keys, never a value, and never the subscription's
connection string. A missing `postgresreplications` CRD is reported on
`/readyz` without failing it. Helm never upgrades CRDs, and a failed readiness
check would take the dashboard down with it.

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
| Replications | Source → target, state, tables ready/subscribed, lag and retained WAL. Each replication shows lag and retained WAL as meters against its own thresholds, with every table and either its sync state or why it is held back. Instance pages add the server's replication readiness; instance and database pages list the replications that use them |

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
| `instanceRef.name` | — | Required. Immutable |
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

### PostgresReplication (namespaced)

One-way logical replication of selected tables from a `PostgresDB` on one
instance into a `PostgresDB` on another. The design is
[`docs/design/replication.md`](docs/design/replication.md).

Every `reconcile.replicationInterval` seconds (60 by default), a pass:

1. **Refuses a replication that can never work.** It checks that both
   `PostgresDB`s exist on different servers, and that both instances admit the
   replication's namespace. Every selected schema must be managed by the
   source. The replication must not close a loop of any length, because a
   subscription republishes what it applies. Both servers must run PostgreSQL
   16 or later. The source must have `wal_level = logical` and a free
   replication slot, and the target's managing role must be able to create
   subscriptions.
2. **Ensures a replication login on the source.** By default this is generated:
   it is granted `rds_replication` so it can stream, and the source's
   `<db>_ro` group so it can read. Its password is in a Secret in the
   operator's namespace. A login supplied with `credentialsSecretRef` is used
   instead and never altered, apart from the `<db>_ro` membership. Adding or
   removing `credentialsSecretRef` later swaps the login: the one it replaces
   is removed once the subscription uses the new one — a generated login and
   its Secret are dropped, a supplied one keeps both.
3. **Converges a publication that lists its tables explicitly.** A table is
   added only once *both* servers can take it. Every other table appears in
   `status.excludedTables` with its reason and remedy.
   - **Reasons that protect the source:** `NoReplicaIdentity`,
     `RowFilterNotCoveredByIdentity`, `ColumnListMissesIdentity` and, on a
     PostgreSQL 18 source, `GeneratedColumnInIdentity`. Publishing such a table
     would make PostgreSQL refuse that table's `UPDATE`s and `DELETE`s on the
     source itself. A `DEFERRABLE` primary key is no replica identity, and a
     table with `REPLICA IDENTITY FULL` takes no column list at all. A
     published table that later loses its replica identity is removed on the
     next pass.
   - **The reason that protects the replica:** `NotPublishable` also holds back
     a partitioned table with an unlogged or foreign partition, whose changes
     logical replication never carries. A published table that gains such a
     partition is taken out rather than left replicating in part, so coming
     back needs an empty target table, or `copyData: false`.
   - **Reasons that keep apply running:** `TargetTableMissing`,
     `TargetColumnsMissing`, `TargetNoReplicaIdentity`, `TargetIdentityNotSent`
     and `TargetColumnRequired`. Apply is sequential, so a single table the
     target cannot receive stops *every* table the subscription carries. Fix
     it on the target - create the table, add the missing columns, give it a
     primary key, narrow its replica identity to what the source publishes as
     its own, or give a `NOT NULL` column a default - and the table is
     published on the next pass.
   - **The reason that protects the target:** `TargetTableNotEmpty`. A table's
     initial copy would collide with rows already on the target, so the
     table waits until it is emptied there, or until `copyData` is false.
4. **Converges the subscription on the target.** It is created with the slot,
   and kept enabled or disabled as the spec says. Its connection is updated
   when the login's password changes. It is refreshed when the publication's
   tables change, so a new table is copied and then streams.
   `status.tables` counts subscribed tables and how many have finished their
   initial copy; the `TABLES` column shows ready/subscribed.

5. **Observes health.** Lag and retained WAL come from the slot on the source.
   The apply worker and the error counts come from the subscription on the
   target. Together they set `status.replicationState`:

   | State | When | Phase |
   | --- | --- | --- |
   | `Streaming` | Every table copied, worker running, under the thresholds | `Ready` |
   | `Initializing` | Some table is still in its initial copy | `Ready` |
   | `Disabled` | `spec.enabled: false` (the slot keeps retaining WAL) | `Ready` |
   | `Lagging` | Lag over `alerting.maxLag`, or retained WAL over `alerting.maxRetainedWal`. Clears below 80% of the threshold, so it does not flap | `Degraded` |
   | `Stalled` | Enabled but the worker is not running, error counts rose, or the slot has had no consumer for `alerting.maxSlotInactive` | `Degraded` |
   | `SlotLost` | The slot is gone, or `max_slot_wal_keep_size` invalidated it. The replica can only be rebuilt: delete and recreate the resource | `Degraded` |

   PostgreSQL counts apply errors but does not expose them, so a `Stalled`
   message points at the target server's log (CloudWatch on RDS). When a
   published table has a target-side problem, the message names it as the
   likely cause. Health keeps being read even when a pass cannot resolve, for
   example after its `PostgresDB` was deleted under `RETAIN`, because the slot
   is still pinning WAL.

```
$ kubectl get postgresreplications -A
NAMESPACE  NAME                 SOURCE           TARGET                        TABLES  LAG     STATE      PHASE  AGE
team-a     orders-to-reporting  prod-rds/orders  reporting-rds/orders_replica  40/41   12 KiB  Streaming  Ready  9d
```

The target server, not the operator, connects to the source. If it cannot, the
replication reports `SourceUnreachableFromTarget`. Set `source.connection.host`
if the target resolves the source differently from the operator.

Every object the operator creates carries an ownership comment and is recorded
in status the moment it exists. An existing object with the same name is never
taken over. An idle slot left by an earlier incarnation is adopted. The slot is
always created from the source before the subscription, never by `CREATE
SUBSCRIPTION` itself. That avoids a deadlock between replications created at
the same time in opposite directions.

Deleting a `PostgresReplication` removes, whatever the `retentionPolicy`:

- the subscription;
- every slot on the source, including table-sync slots;
- the publication;
- the generated login and its Secret.

The finalizer holds until the source's slots are confirmed gone. A slot left
behind retains WAL until the disk fills. If either server is unreachable, the
other is cleared first. The replicated rows on the target are never touched.

A `PostgresDB` with `retentionPolicy: DROP` waits to be dropped while a
replication uses it, in either direction.

| Field | Default | Notes |
| --- | --- | --- |
| `source.dbRef`, `target.dbRef` | — | `name`, `namespace`. Immutable |
| `source.connection` | the source instance's | `host`, `port`, `sslMode` (`require`), `sslRootCertPath`. How the target *server* reaches the source |
| `source.credentialsSecretRef` | generated | A pre-provisioned replication login; read, never written |
| `schemas[]` | — | `name`, `excludeTables`. Every eligible table, re-enumerated each pass |
| `tables[]` | — | `schema`, `name`, `columns`, `rowFilter`. At least one of `schemas`/`tables` |
| `rowFilter` | — | `all` (AND) or `any` (OR) of `{column, op, value \| values}`; `op` is `Eq` `Ne` `Lt` `Le` `Gt` `Ge` `In` `IsNull` `IsNotNull`. Never SQL text |
| `publish` | all four | `insert`, `update`, `delete`, `truncate` |
| `publishViaPartitionRoot`, `copyData`, `enabled` | `false`, `true`, `true` | `publishViaPartitionRoot` is immutable |
| `subscription` | server defaults | `streaming` (`"off"`/`"on"`/`"parallel"` — quote them), `binary` |
| `alerting` | `1Gi`, `10Gi`, `10m` | `maxLag`, `maxRetainedWal`, `maxSlotInactive` |
| `paused` | `false` | |

## Operating notes

### Status and conditions

```bash
kubectl get postgresinstances
kubectl get postgresdbs -A
kubectl get postgresusers -A
kubectl get postgresreplications -A
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
| `Streaming` | `PostgresReplication` only: whether changes flow as they should. Its reason is the replication's state |

`status.phase` collapses these into one printer column: `Ready`, `Drifted`,
`Degraded`, `Unreachable`, `Failed`, `Paused`, `Pending`. `Degraded` is a
`PostgresReplication` that reconciled but whose data is not flowing as it
should: Lagging, Stalled or SlotLost. It outranks `Drifted`.

Every pass that ends `Drifted` or `Degraded` also emits a `Warning` event of
that reason, carrying the condition's message. A `Degraded` replication whose
`Drifted` condition is also true emits both. `pg_operator_resource_ready` is 1
for `Ready` and `Drifted` only, so a `Degraded` replication reads 0.

`Drifted` covers two different situations, and its `reason` says which:

| Reason | Divergence the operator... | Clears `Synced`? |
| --- | --- | --- |
| `RetainedOrphans` | *will not* fix — `retentionPolicy: RETAIN` forbids dropping | No |
| `SchemaNotOwned` | *cannot* fix — the managing role cannot take the schema | Yes |
| `ParameterDenied` | *cannot* fix — the managing role may not set the parameter | Yes |
| `ImmutableFieldDrift` | *cannot* fix — PostgreSQL has no `ALTER` for the field | Yes |
| `DefaultPrivilegesDenied` | *cannot* fix — `setRoleForOwners` is off and the managing role cannot act as an owner-group member it did not create | Yes |
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

pg_operator_replication_lag_bytes{namespace,name}
pg_operator_replication_retained_wal_bytes{namespace,name}
pg_operator_replication_slot_active{namespace,name}
pg_operator_replication_worker_running{namespace,name}
pg_operator_replication_tables{namespace,name,state}       published|subscribed|ready|syncing|excluded
pg_operator_replication_errors{namespace,name,kind}        apply|sync (a gauge: server stats can reset)
pg_operator_replication_state{namespace,name,state}        1 for the current state
pg_operator_replication_observed_timestamp_seconds{namespace,name}
```

A replication's series are removed when it is deleted. With
`metrics.prometheusRule.enabled`, the chart adds alerts. They key off
`pg_operator_replication_state`, so each replication's own thresholds decide
when they fire:

- `SlotLost` fires immediately, as critical.
- `Stalled` and `Lagging` fire after `metrics.prometheusRule.for`.
- Rising error counts raise a warning.
- So does a replication nothing has observed for three intervals. That means
  the operator, or its access to the servers, is down, and the slot is
  unwatched.

### Logical replication readiness

Every instance reports, on each pass, whether its server could take part in
logical replication, as a source or a target. None of this is the operator's
to set: `wal_level` and the slot
limits live in the RDS parameter group, and the replication privilege is an
AWS-managed role. So the operator reads it and reports it, and it never makes
an instance unhealthy.

```console
$ kubectl get postgresinstance prod-rds -o jsonpath='{.status.logicalReplication}' | jq
{
  "walLevel": "replica",
  "maxReplicationSlots": 20,
  "replicationSlotsInUse": 0,
  "maxWalSenders": 35,
  "maxSlotWalKeepSize": "-1",
  "createSubscription": "Assumed",
  "replicationGrant": "rds_replication",
  "issues": [
    "wal_level is replica; set rds.logical_replication = 1 in the DB parameter group and reboot (static parameter)"
  ]
}
```

- **`createSubscription`** says whether the managing role may own a
  subscription.
  - `Granted`: it is a member of `pg_create_subscription`, or a superuser.
  - `Assumed`: RDS. AWS documents the master user creating subscriptions but
    not the mechanism, and `rds_superuser` is not a member of
    `pg_create_subscription`.
  - `Unavailable`: it may not. The server can still be a source.
- **`replicationGrant`** says how the managing role can let a login stream: by
  granting `rds_replication`, by conferring `REPLICATION` itself (`attribute`),
  or not at all (`none`).
- **`maxSlotWalKeepSize: "-1"`** means a stalled subscriber can retain WAL until
  the disk fills. Setting it in the parameter group is the backstop the
  operator deliberately is not.

### Running a replication

- **Change schemas on the target first when adding, and on the source first
  when removing.** A column the source sends and the target lacks stops the
  apply worker, and with it every table. A column only the target has is
  harmless. Rename in two steps: add and backfill, then drop.
- **Set `max_slot_wal_keep_size`** in the source's parameter group. Without it
  a stalled subscriber retains WAL until the source's disk fills. The operator
  reports and alerts but never drops a slot to save the disk. With the
  backstop set, a slot that falls too far behind is invalidated instead, and
  the replication reports `SlotLost`.
- **Open the network path between the two servers.** The target's security
  group must allow egress to the source, and the source's must allow ingress
  from the target. The operator reaching both servers proves nothing about it.
- **Delete the replication before a major-version upgrade or a blue/green
  deployment.** At the time of writing, AWS's upgrade precheck refuses an
  instance with logical slots, and slots survive only upgrades from 17. The
  blue environment of a blue/green deployment cannot publish or subscribe.
  Recreate the replication afterwards; it copies every table afresh.
- **Recovering from `SlotLost`** also means deleting and recreating the
  resource, and so a full re-copy.
- **Sequences are not replicated.** That does not matter for a read replica.
  It would if the replica were ever promoted, which this does not support.
- **`TRUNCATE` replicates.** On the target it fails if a table that is not
  replicated references the truncated one by foreign key. Drop `truncate` from
  `publish` in that case.
- **The replication password lives on the target.** It is stored in the
  target's `pg_subscription` catalog, and appears in the target's statement
  log when `log_statement` is `ddl` or `all`. The login can read only what the
  source's `<db>_ro` can.

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
authentication of the operator's own connections.

```bash
curl -o rds-ca.pem https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem
kubectl -n pg-operator create secret generic rds-ca-bundle --from-file=ca.pem=rds-ca.pem
```

### Logical replication on RDS

- **The source needs `rds.logical_replication = 1`** in its parameter group.
  It is a static parameter, so it takes a reboot. The instance's
  `status.logicalReplication` says whether it is set.
- **Streaming goes through `rds_replication`.** The master user cannot confer
  the `REPLICATION` attribute, so it grants the generated login
  `rds_replication`, which RDS honours for streaming.
- **The master user creates subscriptions.** AWS documents that it can, but
  not how: `rds_superuser` is not a member of `pg_create_subscription`. The
  readiness block reports this as `createSubscription: Assumed`.
- **The replication's TLS defaults to `require`.** The target *server* opens
  the connection to the source, and AWS documents no CA path an RDS subscriber
  could verify against. `verify-full` needs an explicit
  `source.connection.sslRootCertPath` on the target server.

> **Not yet confirmed on real RDS.** `PostgresReplication` is tested against
> PostgreSQL 16–18 servers that emulate the RDS master user, not against RDS
> itself. The points above are what AWS documents or what the emulation
> assumes. Before relying on replication between RDS instances, run the
> [real-RDS verification gate](docs/design/replication.md#real-rds-verification-gate)
> on an RDS 16 or 17 pair. It checks subscription and slot privileges,
> streaming through `rds_replication` alone, TLS, what reaches the
> CloudWatch statement log, and Multi-AZ failover.

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
make test-setup         # two servers: a source on :15432, a target on :15433
make test-integration   # the whole suite against them
make test-teardown

make test-setup PG_IMAGE=postgres:18-alpine PG_TARGET_IMAGE=postgres:16-alpine
```

`make test-setup` runs `hack/test-servers.sh`, which bootstraps both servers
with `hack/rds-like.sql`: an RDS-like managing role `pgadmin` that is a member
of `rds_superuser` with `CREATEDB` and `CREATEROLE`, not a superuser. The same
file bootstraps CI. Both servers run `wal_level=logical`, and they share a
Docker network so the target's server can subscribe to the source's. It prints
the variables the suite reads:

| Variable | Used for |
| --- | --- |
| `PGOP_TEST_DSN` | The source, and every single-server test |
| `PGOP_TEST_TARGET_DSN` | The replication target |
| `PGOP_TEST_SOURCE_FROM_TARGET` | The source *as the target server reaches it* (`host=pgop-test port=5432`) |
| `PGOP_TEST_SUPERUSER_DSN` | One thing only: emulating RDS's `rds_replication` (below) |

Without `PGOP_TEST_DSN` the integration tests skip and the unit tests still run.
Tests marked `replication` (`uv run pytest -m replication`) also need the
other three, and skip below PostgreSQL 16. CI runs them in their own matrix of
source → target pairs: 16→16, 17→17, 18→18, 16→18 and 18→16.

**The one superuser step.** On RDS, a login granted `rds_replication` may
stream from a replication slot. Stock PostgreSQL honours only the
`REPLICATION` attribute, which a non-superuser cannot confer.
`hack/rds-replication-shim.sql` bridges the two: run as the container
superuser, it gives `REPLICATION` to every login that is a *direct* member of
`rds_replication`. The managing role is only an indirect member, through
`rds_superuser`, exactly as the RDS master user is. It therefore keeps
`NOREPLICATION`, so the harness still surfaces the bug class that attribute
causes. Everything else runs as `pgadmin`.
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
make local-status    # phases for all four kinds
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
    replication.py   what a server can take part in (wal_level, slots, rights)
    publication.py   the catalog facts a table is judged on; publication DDL
    subscription.py  subscriptions, slots, and their health
    filters.py       the row-filter DSL, composed as SQL
  units.py           Kubernetes quantities and durations; byte formatting
  k8s/               client, cross-namespace Secrets, PushSecret, CR access
  reconcile/         convergence + teardown for each kind; for replication also
                     eligibility.py (which tables may be published) and
                     health.py (the state a replication's numbers put it in)
  handlers/          kopf registration, retry mapping, metrics
  api/               FastAPI state API
```

### Building the image

```bash
docker build -t pg-operator:dev .
```

Multi-stage, `python:3.14-slim`, uv-installed, non-root (65532), read-only root
filesystem. `psycopg[binary]` bundles libpq, so no PostgreSQL client packages
are needed. `make build-all` builds both images; `make ui-image-check` runs the
dashboard's under the pod's own constraints.

### Publishing

The `image` job in `.github/workflows/ci.yaml` builds both images, runs every
assertion against them, and only then pushes to `ghcr.io/<owner>/<repo>` and
`ghcr.io/<owner>/<repo>-ui`. It authenticates with the workflow's own
`GITHUB_TOKEN`, so there is no secret to configure. Nothing is pushed for a
pull request: a fork's token has no `packages: write`, and an unreviewed branch
should not be able to publish.

| Trigger | Tags pushed |
| --- | --- |
| push to `main` | `main`, `sha-<full sha>`, `latest` |
| tag `v1.2.3` | `1.2.3`, `1.2`, `sha-<full sha>` |
| pull request | none — built and tested only |

A `v*` tag drops its `v`, so tagging `v0.1.0` publishes `:0.1.0` — which is
exactly what the chart resolves to when `image.tag` is empty and `appVersion`
is `0.1.0`. Keep `Chart.yaml`'s `version` and `appVersion` in step with the git
tag and the chart's defaults stay correct with no override.

Both images are published for `linux/amd64` and `linux/arm64`, so the same tag
runs on Graviton and Intel nodes alike. The `image` job is a matrix over two
native runners — `ubuntu-latest` and `ubuntu-24.04-arm` — rather than one
runner emulating the other under QEMU: a `uv sync` and a `vite` build cost far
more emulated than a second runner costs, and building natively means the
assertions above run against the arm64 image too, not only the amd64 one.

Each runner pushes an untagged, single-arch image with `push-by-digest` and
reports its digest as an artifact. A final `image-manifest` job joins the pair
into one tagged manifest list with `docker buildx imagetools create`, then
asserts that both architectures resolve under the tag. Tags are applied only
there, so a tag never points at a half-published pair: if either architecture
fails to build, no tag moves at all. To add a platform, extend the matrix with
its runner — nothing else in the job is architecture-specific.

A GHCR package inherits the repository's visibility the first time it is
published. If the repository is private, so are the packages, and the cluster
needs a pull Secret — see the chart README.

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
- **Replication publishes an explicit table list.** `FOR TABLES IN SCHEMA`
  would publish a table the moment it is created on the source, and its first
  write would stop the whole subscription if the target lacked it. An explicit
  list adds a table only once both servers can take it, and needs only table
  ownership, which works the same on RDS.
- **Replication teardown ignores `retentionPolicy`.** A slot left behind
  retains WAL until the source's disk fills, so deleting a `PostgresReplication`
  always removes the plumbing, and always keeps the replicated rows.
- **Slot safety is reported, not enforced.** The operator alerts on lag,
  retained WAL and an idle slot, but never drops a slot to protect the source.
  `max_slot_wal_keep_size` is that backstop, and it belongs in the parameter
  group.
- **No password rotation in this iteration.** Deliberate: rotation under a
  running application needs an explicit, auditable trigger, which is the next
  increment rather than a default.

## License

Apache-2.0
