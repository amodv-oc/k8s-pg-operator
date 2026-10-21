# PostgresReplication: logical replication between instances

| | |
| --- | --- |
| Status | Accepted 2026-10-21, D1–D11 included. PRs 1–4 merged; PRs 5–7 (health, state API, dashboard) built together |
| Date | 2026-10-21 |
| Scope | Long-lived, one-way logical replication from a database on one `PostgresInstance` into a database on another |

## Contents

- [Summary](#summary)
- [Decisions taken](#decisions-taken)
- [Non-goals](#non-goals)
- [Background](#background)
- [The resource](#the-resource)
- [What the operator creates](#what-the-operator-creates)
- [Reconciliation](#reconciliation)
- [Health](#health)
- [Teardown](#teardown)
- [Interaction with the existing kinds](#interaction-with-the-existing-kinds)
- [Surfaces](#surfaces)
- [Testing](#testing)
- [Delivery plan](#delivery-plan)
- [Decisions for review](#decisions-for-review)
- [Operating guidance](#operating-guidance)
- [Follow-ups](#follow-ups)
- [References](#references)

## Summary

A fourth custom resource, `PostgresReplication`, declares that a selection of
tables in one `PostgresDB` is continuously replicated into another `PostgresDB`
on a different instance, using PostgreSQL's built-in logical replication.

The operator owns the replication **plumbing** — a publication, a dedicated
replication login, a subscription and its slot — and nothing else. It never
creates or alters tables. It checks that the target can receive each selected
table, publishes only those that can, and reports the rest by name with a
remedy. Health — lag, WAL retained on the source, apply errors, per-table sync
state — is reported through status, Prometheus, the state API and the
dashboard.

```
  prod-rds                                      reporting-rds
  ┌───────────────────────────────┐             ┌──────────────────────────────┐
  │ orders                        │             │ orders_replica               │
  │  publication pgop_team_a_…  ◀─┼─ explicit   │  subscription pgop_team_a_…  │
  │   public.*, billing.invoices  │  table list │   owned by the master user   │
  │  slot pgop_team_a_…           │             │   applies as each table's    │
  │  login pgop_repl_team_a_…   ◀─┼─────────────┼── owner (<db>_owner)         │
  │   rds_replication + orders_ro │  walsender  │                              │
  └───────────────────────────────┘  connection └──────────────────────────────┘
          ▲                          (target server → source server)       ▲
          │ SQL as master user                              SQL as master user
          └──────────────────────── pg-operator ───────────────────────────┘
```

## Decisions taken

Settled in the planning Q&A on 2026-10-21:

| Question | Decision |
| --- | --- |
| Purpose | Long-lived replica: reporting, analytics, a cross-region read copy. Migration and cutover are out of scope |
| Mechanism | PostgreSQL logical replication only. RDS read replicas are an AWS API operation, not SQL, and stay out of scope |
| Selection | Schemas, individual tables, row filters and column lists |
| Resource shape | One namespaced `PostgresReplication` that references a source and a target `PostgresDB` |
| Target DDL | Verify and report. The operator never creates target tables; migrations run against both sides |
| Minimum version | PostgreSQL 16, on both source and target |
| Deletion | Always removes the subscription, the slot, the publication and the replication login. Replicated data on the target is always kept. `retentionPolicy` does not apply |
| Slot safety | Report and alert only. The operator never drops a slot to protect the source |
| Target writes | Not restricted. Apply conflicts are reported, not prevented |
| Namespaces | The same trust model as `PostgresUser`: cross-namespace refs are allowed, gated by each instance's `allowedNamespaces` |
| Surfaces | Status and print columns, Prometheus metrics, state API, dashboard |

Calls this document made that the Q&A did not cover are collected in
[Decisions for review](#decisions-for-review). All eleven were accepted on
2026-10-21.

## Non-goals

- **Migration and cutover.** No sequence synchronisation, no promotion, no
  write-freeze orchestration.
- **Bidirectional replication.** This rules out `origin = none` and any form of
  conflict resolution.
- **Physical replication.** This includes RDS read replicas.
- **DDL on the target.** The operator neither creates nor alters tables there.
- **Replication within one server.** See D10.
- **Failover slots.** PostgreSQL 17's `failover = true` is left for a follow-up.
- **Server parameters.** `rds.logical_replication`, `max_slot_wal_keep_size`,
  `max_replication_slots` and `max_wal_senders` live in the RDS parameter group,
  which the operator does not own. It reads them and reports them, but never
  sets them.

## Background

### Moving parts

| Object | Lives in | Role |
| --- | --- | --- |
| Publication | source database | Declares which tables, which operations, and which rows and columns are published |
| Replication slot | source server | Holds WAL until the subscriber confirms it. One per subscription |
| Replication login | source server | The role the **target server** connects as. Needs the replication privilege and `SELECT` on the published tables |
| Subscription | target database | Holds the connection string. Runs the apply worker, plus table-sync workers during the initial copy |

Two properties shape almost everything below.

1. **The target server connects to the source server, not the operator.**
   - The network path, the hostname and TLS must all work from the target
     server's side.
   - The replication password ends up in the target's `pg_subscription`
     catalog.
   - The operator can check that it can reach both servers itself. It can only
     find out whether the target can reach the source by trying.
2. **A slot pins WAL on the source until the subscriber consumes it.** If the
   subscriber stops, the slot keeps the source's WAL growing until something
   intervenes. On RDS, `max_slot_wal_keep_size` defaults to `-1` (unlimited), so
   the thing that intervenes is a full disk.

### Privileges, for a managing role that is not a superuser

Evidence levels: **D** means documented by PostgreSQL or AWS. **3P** means
third-party or AWS-blog evidence only. **?** means undocumented or inferred.
Every **?** row is something the [real-RDS gate](#real-rds-verification-gate)
must confirm.

| Operation | Stock PostgreSQL 16 rule | RDS master user | Local harness emulation |
| --- | --- | --- | --- |
| `CREATE PUBLICATION … FOR TABLE` | `CREATE` on the database, plus ownership of each table (D) | Same. The operator gets both through its `INHERIT` grant on `<db>_owner` | None needed |
| `… FOR ALL TABLES` / `FOR TABLES IN SCHEMA` | Superuser only (D) | `FOR ALL TABLES` works (3P). `IN SCHEMA` is undocumented (?) | Impossible. Not used: see [D1](#decisions-for-review) |
| Create the replication login | `CREATEROLE` | Same | None needed |
| Let that login stream (walsender) | `REPLICATION` role attribute (D). A `CREATEROLE` role cannot grant it | `GRANT rds_replication` (D for slots; 3P for subscriptions) | A test-only shim sets `REPLICATION` on logins that are *direct* members of an emulated `rds_replication`. The managing role, an indirect member, keeps `NOREPLICATION` |
| `SELECT` for the initial copy | `SELECT` on each table (D) | Membership of the source's `<db>_ro` | None needed |
| `CREATE SUBSCRIPTION` | `pg_create_subscription`, plus `CREATE` on the database (D) | Works as `rds_superuser`, though the mechanism is undocumented (3P/?) | Bootstrap runs `GRANT pg_create_subscription TO rds_superuser` |
| Apply changes as the table owner | `SET` on each target table's owner role (D, PostgreSQL 16 with `run_as_owner = false`) | The operator already grants itself `SET` on `<db>_owner` (`ensure_self_membership`) | None needed |
| Full rows in `pg_stat_replication` | `pg_read_all_stats` | `rds_superuser` holds `pg_monitor` (D) | Bootstrap runs `GRANT pg_monitor TO rds_superuser` |
| `pg_drop_replication_slot` | `REPLICATION` | Through `rds_replication` (D) | Done as the replication login |
| Read `pg_subscription.subconninfo` | Superuser only (D) | Assume not (?) | None. The operator never reads it |

### PostgreSQL behaviours the design depends on

All of the following come from PostgreSQL documentation or source.

- **A published table the target lacks stops the whole subscription.** The
  apply worker opens the target relation for every change it receives. A
  missing relation, or a missing column, is an error, and apply is sequential,
  so one such change stops **every** table. This is not limited to tables
  already being synced: it happens as soon as the source writes to the table.
- **A wrong replica identity breaks the source, not the replica.** The
  publisher rejects an `UPDATE` or `DELETE` on a published table in three cases:
  - the table has no usable replica identity;
  - a row filter references a column outside the replica identity;
  - a column list does not cover the replica identity.

  The check happens at DML time, not when the publication is altered.
  Publishing the wrong table therefore takes the source application's writes
  down.
- **`DROP DATABASE` is blocked by replication objects.** It refuses a database
  that has subscriptions, or that has an *active* logical slot.
- **`ALTER SUBSCRIPTION … REFRESH PUBLICATION` cannot run in a transaction
  block.** Nor can `DROP SUBSCRIPTION` while the subscription has a slot. The
  operator's pools are already autocommit.
- **`DROP SUBSCRIPTION` drops the remote slot.** It fails if the publisher is
  unreachable. The documented escape is `DISABLE`, then `SET (slot_name =
  NONE)`, then `DROP`, which leaves the slot behind on the source.

## The resource

```yaml
apiVersion: postgres.ourcommunity.com.au/v1alpha1
kind: PostgresReplication
metadata:
  name: orders-to-reporting
  namespace: team-a
spec:
  source:
    dbRef: {name: orders}                       # a PostgresDB on prod-rds
    connection:                                 # how the TARGET SERVER reaches the source
      host: prod.abc123.ap-southeast-2.rds.amazonaws.com   # default: source instance host
      port: 5432                                # default: source instance port
      sslMode: require                          # default
  target:
    dbRef: {name: orders-replica, namespace: analytics}    # a PostgresDB on reporting-rds
  schemas:
    - name: public
      excludeTables: [sessions, rate_limits]
    - name: audit
  tables:
    - schema: billing
      name: invoices
      columns: [id, region, customer_id, total, issued_at, voided_at]
      rowFilter:
        all:
          - {column: region, op: In, values: [AU, NZ]}
          - {column: voided_at, op: IsNull}
  publish: [insert, update, delete, truncate]   # default: all four
  copyData: true                                # initial copy, and for tables added later
  enabled: true                                 # false = ALTER SUBSCRIPTION ... DISABLE
  subscription:
    streaming: parallel                         # off | on | parallel; omitted = server default
    binary: false
  alerting:
    maxLag: 1Gi
    maxRetainedWal: 10Gi
    maxSlotInactive: 10m
```

### Fields

| Field | Default | Notes |
| --- | --- | --- |
| `source.dbRef`, `target.dbRef` | — | `name`, `namespace` (defaults to the resource's own). **Immutable.** They must resolve to `PostgresDB`s on two **different** instances |
| `source.connection.host` / `port` | source instance's | The endpoint as seen from the target server. Often a private DNS name the operator never uses itself |
| `source.connection.sslMode` | `require` | `verify-ca`/`verify-full` also need `sslRootCertPath`, a path **on the target server's filesystem**. See D11 |
| `source.credentialsSecretRef` | — | Bring your own replication login, pre-provisioned with the replication privilege. Read only, never written. See D9 |
| `schemas[]` | `[]` | `name`, `excludeTables[]`. Every eligible table in the schema is published. The list is re-enumerated on every pass, so a new table is picked up automatically once it qualifies |
| `tables[]` | `[]` | `schema`, `name`, optional `columns[]` and `rowFilter`. An explicit entry overrides the table's `schemas[]` membership. A table both listed here and excluded in `schemas[]` is refused as contradictory |
| `rowFilter` | — | `all:` (AND) or `any:` (OR), one level deep. Each predicate is `{column, op, value \| values}`. `op` is one of `Eq` `Ne` `Lt` `Le` `Gt` `Ge` `In` `IsNull` `IsNotNull`. See D2 |
| `publish` | all four | Subset of `insert`, `update`, `delete`, `truncate` |
| `publishViaPartitionRoot` | `false` | Passed through to the publication. **Immutable**: switching it swaps the relations the subscription carries (the root for its leaves, or back), and the next refresh would copy the newcomers over rows the target already holds |
| `copyData` | `true` | Used on create and on every refresh that adds tables |
| `enabled` | `true` | `false` disables the subscription. The slot keeps retaining WAL while it is disabled, so this is a maintenance lever, not a parking state |
| `subscription.streaming`, `subscription.binary` | server default | Passed through to `CREATE`/`ALTER SUBSCRIPTION` |
| `alerting.maxLag` | `1Gi` | Bytes the target has not yet confirmed |
| `alerting.maxRetainedWal` | `10Gi` | WAL the slot pins on the source |
| `alerting.maxSlotInactive` | `10m` | How long the slot may have no consumer before the replication counts as stalled |
| `paused` | `false` | Same as the other kinds: no connection is opened, and the resource reports `Paused` |

Settings that change the subscription itself — `run_as_owner`, `origin`,
`two_phase`, `disable_on_error`, `password_required` — are deliberately not
exposed. They stay at PostgreSQL's defaults, which are the safe values for a
non-superuser owner.

### Admission rules (CEL)

- `source.dbRef`, `target.dbRef` and `publishViaPartitionRoot` are immutable
  (`self == oldSelf`).
- A `PostgresDB`'s `instanceRef` is immutable too. `dbRef` alone does not pin
  an end: moving the `PostgresDB` would move every replication of it.
- At least one entry in `schemas` or `tables`.
- No `(schema, name)` appears twice in `tables`. No schema appears twice in
  `schemas`.
- `rowFilter` sets exactly one of `all` or `any`. `values` is set for `In` only,
  and `value` is set for every other operator except the null tests. Operands
  are int-or-string. A YAML boolean is refused rather than coerced, because a
  literal of unknown type is compared as the column's own type.
- Identifier fields match the pattern the other CRDs already use.

### Checks the reconciler makes

These are reported as **permanent** errors:

- Source and target resolve to the same instance, or to two instances with the
  same `host:port`.
- Either server is older than PostgreSQL 16.
- An end resolves to a different instance or database from the one status
  records objects on, for example because its `PostgresDB` was deleted under
  `RETAIN` and recreated elsewhere under the same name. Converging there would
  create a second publication, login and slot and overwrite status with them,
  and teardown, which works from status alone, would never find the old slot
  again. Refusing keeps status intact, so the old slot stays watched and
  deleting the replication still removes it.
- A `schemas[]` or `tables[]` entry names a schema that is not managed by the
  source `PostgresDB`. The replication login reads through `<db>_ro`, which
  holds grants only on managed schemas.
- The replication would close a **cycle of any length**. A subscription
  republishes what it applies, so A→B→C→A loops changes just as A→B beside B→A
  does, until apply stalls on a duplicate key. The graph's nodes are physical
  databases (`host`, `port`, database name), so two `PostgresDB`s naming one
  database cannot hide a loop. A replication that is being deleted, or cannot
  resolve, adds no edge. The check is at database level, so two databases
  exchanging *disjoint* tables are refused too. That is consistent with the
  non-goal of bidirectional replication.

## What the operator creates

| Object | Where | Name | Created by |
| --- | --- | --- | --- |
| Replication login | source server | `{rolePrefix}pgop_repl_{ns}_{name}_{hash}` | operator, unless `credentialsSecretRef` is set |
| Credentials Secret | operator namespace | `pgop-repl.{ns}.{name}` | operator, for a generated login only |
| Publication | source database | `pgop_{rolePrefix}{ns}_{name}_{hash}` | operator |
| Slot | source server | same as the subscription | operator, from the source, before the subscription — never `CREATE SUBSCRIPTION` itself (see step 6) |
| Subscription | target database | `pgop_{rolePrefix}{ns}_{name}_{hash}` | operator |

Naming rules:

- **Name construction** (`naming.replication_names`).
  - **The hash.** Mapping `-` and `.` to `_` loses the boundary between
    namespace and name, so `team-a/x` and `team/a-x` would sanitise alike. Every
    identifier therefore ends in an 8-character hash of the exact
    `rolePrefix`/namespace/name. The readable part is truncated so the whole
    name fits in 63 bytes.
  - **The Secret name.** A namespace is a DNS label and cannot contain a dot,
    so `pgop-repl.{ns}.{name}` is unambiguous without a hash. One is added only
    if the name would exceed 253 characters.
  - **Prefixes.** The `pgop_` prefix keeps slot names out of RDS's reserved
    `rds*` namespace and away from `pg_`. Including `rolePrefix` keeps two
    environments that share a source server from colliding.
- **Names are recorded, not re-derived.** Each name is written to status when
  the object is created. Every later pass and the teardown use the recorded
  name, so a future change to the naming scheme cannot orphan an object.
- **Ownership markers.** The publication, subscription and generated login each
  get `COMMENT ON … IS 'pg-operator: PostgresReplication <ns>/<name>'`. Suppose
  an object with the derived name already exists with a different marker, or
  none at all. The operator does not take it over: it reports a permanent
  `NameConflict`. An object carrying this resource's own marker is adopted,
  which covers a CR that was force-deleted and then recreated.
- **Only operator-created objects are dropped.** `status.created` records which
  objects the operator made, and teardown drops only those. This mirrors
  `status.managedSchemas`.

## Reconciliation

Every pass runs the same convergence code, whether it came from create, update,
resume or the timer. Steady state issues no DDL.

1. **Resolve.** Load both `PostgresDB`s and both instances, enforcing
   `allowedNamespaces` on each instance for this resource's namespace. Check the
   version floor and the same-instance rule.
2. **Prerequisites.**
   - The source must report `wal_level = logical`. If it does not, the
     replication fails permanently with `LogicalReplicationDisabled` and the
     remedy "set `rds.logical_replication = 1` and reboot".
   - Before a subscription is created, the source must have a free
     replication slot. If it does not, the pass fails with
     `InsufficientCapacity` (temporary) rather than an opaque `CREATE
     SUBSCRIPTION` failure. Its table-sync workers briefly want more slots
     during the initial copy, but PostgreSQL retries those itself.
   - The target's managing role must be able to create subscriptions
     (`status.logicalReplication.createSubscription` is not `Unavailable`).
3. **Replication login** (source).
   - **Generated login:**
     - Create it as `LOGIN` with a generated password.
     - Write the credentials Secret, using the existing `generate_password` and
       `apply_secret`.
     - Confer the replication privilege the way the instance's
       `status.logicalReplication.replicationGrant` says:
       - `rds_replication`: `GRANT rds_replication`.
       - `attribute`: `ALTER ROLE … REPLICATION`. This is a superuser-managed
         stock server.
       - `none`: the replication fails permanently with
         `ReplicationPrivilegeUnavailable`, and the error points at
         `credentialsSecretRef`.
   - **Bring-your-own login:** read the Secret.
   - **Either kind:** grant membership of the source's `<db>_ro`, which supplies
     `CONNECT`, `USAGE` and `SELECT`. Verify the grant by reading it back.
   - **Self-healing:** if the Secret is deleted, generate a new password, apply
     it, and let step 6 push the new connection string. As with `PostgresUser`,
     this is not rotation.
4. **Resolve the table set** (source catalog). Expand `schemas[]` minus
   `excludeTables`, then overlay `tables[]`.
   - **Kept:** permanent ordinary tables (`relkind = 'r'`) and partitioned roots
     (`'p'`).
   - **Left out:** leaf partitions, which their root covers; unlogged, temporary
     and foreign tables; views; materialised views.
   - **Explicit entries:** an explicit entry for a kind of object that cannot be
     published is reported as `NotPublishable`.
5. **Gate each table, then converge the publication** (source).
   - Run every candidate through [table eligibility](#table-eligibility).
   - Membership is an explicit `FOR TABLE` list (see
     [D1](#decisions-for-review)), converged one table at a time:
     - **New tables:** `ALTER PUBLICATION … ADD TABLE`.
     - **Removed or failing tables:** `DROP TABLE`.
     - **Changed column list or filter:** `DROP` then `ADD`, in one
       transaction. A filter the server refuses therefore leaves the previous
       shape published, rather than the table out.
   - A table whose `ADD` the server refuses is held back as `InvalidRowFilter`
     (it has a filter) or `PublicationRefused` (it has none); every other
     table proceeds.
   - Converge `publish` and `publish_via_partition_root` with `ALTER
     PUBLICATION … SET`.

**Order within a pass, and why.** Steps 3–5 create objects on the source, so:

- **Names are checked first.** Before anything is created, a read-only pass
  checks the publication's and the generated login's names. A pass that can
  never succeed, because another object holds the name, creates nothing.
- **Each creation is recorded immediately.** The publication (created empty)
  and then the login are each written to `status.created` with a status patch
  the moment they exist. A pass that fails afterwards publishes only its
  conditions, so without this an object made early in it would be unknown to
  teardown. It would then outlive a deletion that arrived before the next
  successful pass. A test fails a pass mid-way and proves teardown still finds
  everything.
6. **Converge the subscription** (target).
   - Create it if it is missing, in two steps:
     1. **The slot.** It is created from the source with
        `pg_create_logical_replication_slot`, in a transaction of its own and
        connected to the source database, since a logical slot belongs to one
        database. An idle slot of this name, in that database, is this
        replication's own leftover and is adopted. One in use, or in another
        database, is reported and never touched.
     2. **The subscription.** `CREATE SUBSCRIPTION … CONNECTION … PUBLICATION
        … WITH (create_slot = false, slot_name, copy_data, streaming, binary,
        enabled)`.
   - **Why the slot is never made by `CREATE SUBSCRIPTION`.** `CREATE
     SUBSCRIPTION` would create the slot inside its own transaction *on the
     target*, and creating a slot waits for every transaction already running
     on the source. Two replications created at once in opposite directions
     between two servers would then each wait on the other until the
     statement timeout, and retry into the same deadlock. Three replications
     round a ring of servers do the same. The local cluster harness found
     this: 16→17, 17→18 and 18→16 deadlocked on every retry. Made from the
     source, the slot waits only for that server's own transactions. A test
     creates two opposite subscriptions at once, and fails on the old path.
   - Otherwise:
     - Apply `ENABLE`/`DISABLE` from `spec.enabled`.
     - Apply `SET (streaming, binary)`.
     - Run `CONNECTION …` only when the connection fingerprint changes.
     - Run `REFRESH PUBLICATION WITH (copy_data)` only when the published table
       set differs from `pg_subscription_rel`.
7. **Observe.** See [Health](#health).
8. **Publish status.**

### Why the publication lists tables explicitly

`FOR TABLES IN SCHEMA` looks like the natural fit for `schemas[]`. It is not
used, for three reasons.

1. **It is unsafe against migrations.** Under `IN SCHEMA`, a table created on
   the source joins the publication the moment it exists. Its first write
   reaches the apply worker, which stops the **entire** subscription if the
   target does not have the table yet. With an explicit list, the operator
   publishes a table only after the target has it with compatible columns. A
   source-first migration then leaves that one table unpublished and reported,
   rather than stalling every table.
2. **It saves nothing.** The subscriber still has to run `REFRESH PUBLICATION`
   before a new table is copied, and only the operator runs that. Pick-up
   latency is one reconcile interval either way.
3. **It is not available.** It needs a superuser on stock PostgreSQL, and it is
   undocumented for `rds_superuser`. The harness cannot prove it. An explicit
   list needs only table ownership, which the operator holds through
   `<db>_owner`.

### Table eligibility

A table is published only when every check passes. A failure excludes that
table alone. The table is listed in `status.excludedTables` with a reason and a
remedy, the rest of the replication converges, and `Drifted` (and `Synced=False`)
names the cause. This is the same "report, don't abort" rule as
`SchemaNotOwned` and `ParameterDenied`.

| Check | Side | Reason | Why it is a gate |
| --- | --- | --- | --- |
| Usable replica identity (primary key, `USING INDEX`, or `FULL`) when `publish` includes `update` or `delete`. A `DEFERRABLE` primary key does not count: PostgreSQL never picks one as the identity | source | `NoReplicaIdentity` | Without it, every `UPDATE` and `DELETE` on the source fails |
| Row-filter columns ⊆ replica-identity columns (unless `FULL`), under the same condition | source | `RowFilterNotCoveredByIdentity` | Same failure on the source |
| Column list ⊇ replica-identity columns, and no column list at all on a `FULL` table, under the same condition | source | `ColumnListMissesIdentity` | Same failure on the source. PostgreSQL allows no column list with `REPLICA IDENTITY FULL`, even one naming every column |
| On a PostgreSQL 18+ source, the replica identity holds no generated column (`FULL` counts every column), under the same condition | source | `GeneratedColumnInIdentity` | PostgreSQL 18 refuses `UPDATE` and `DELETE` when the identity takes in a generated column the publication does not carry. The operator never publishes generated columns, and a virtual one cannot be published at all |
| Every leaf of a partitioned table is a permanent, local table | source | `NotPublishable` | An unlogged leaf writes no WAL and a foreign one keeps its rows elsewhere, so neither's changes are ever decoded and the table would replicate only in part, silently. Like `RowSecurityEnabled`, this is source-side for completeness rather than for the source's writes: a published table that gains such a leaf is taken out, so all its leaves stop replicating, and coming back means an initial copy into a target that must be empty (or `copyData: false`) |
| Listed columns exist and are not generated | source | `UnknownColumn` | Otherwise `ALTER PUBLICATION` fails |
| Managing role has privileges of the table owner | source | `SourceTableNotOwned` | `ALTER PUBLICATION … ADD TABLE` needs ownership. The remedy names `ALTER TABLE … OWNER TO <db>_owner` |
| Replication login can `SELECT` the table | source | `SourceTableNotReadable` | The initial copy runs as that login |
| Row-level security is off | source | `RowSecurityEnabled` | The initial copy runs as a non-owner and is subject to policies, but the change stream is not. Such a replica would be silently partial |
| Table exists on the target with the same schema and name (and the same leaves, for a partitioned root published via leaves) | target | `TargetTableMissing` | Otherwise apply stops for every table |
| Every published column exists on the target and is not generated there | target | `TargetColumnsMissing` | Same |
| Managing role can `SET ROLE` to the target table's owner | target | `TargetOwnerNotSettable` | PostgreSQL 16 applies changes as the table owner |
| The target can identify a row: a primary key that is not `DEFERRABLE`, or a `REPLICA IDENTITY` index — unless the source publishes `REPLICA IDENTITY FULL` | target | `TargetNoReplicaIdentity` | "If a replica identity other than FULL is set on the publisher side, a replica identity comprising the same or fewer columns must also be set on the subscriber side." Without one, apply stops for every table. A target set to `FULL` does not count: the subscriber searches through an index, never the whole row. Nor does a deferrable primary key, which the apply worker never searches by. Before PostgreSQL 18 apply then stops; 18 instead skips each `UPDATE` and `DELETE` as a missing row, which is worse, because it is silent |
| The target's identity columns ⊆ the columns the source publishes *as its replica identity* | target | `TargetIdentityNotSent` | The publisher sends only its own identity as the key, however many columns it publishes. Same consequence |
| Every `NOT NULL` column on the target that the publication does not carry has a default, or is an identity or generated column | target | `TargetColumnRequired` | Otherwise every `INSERT` the subscription applies fails the constraint, from the initial copy onwards |
| No other `PostgresReplication` targets the same table | target | `TableTargetedTwice` | Two writers to one replica table conflict. The older replication keeps the table, by creation time and then namespace/name, so the two can never each defer to the other. It is the one target-side reason that takes an already-published table out |
| With `copyData`, a table not yet subscribed is empty on the target | target | `TargetTableNotEmpty` | Its initial copy would collide with the rows already there and stall apply. The case D6 makes easy: a table unpublished when its replica identity went, then fixed, would otherwise be copied over its own replicated rows |
| A partitioned table's column list or row filter is set only with `publishViaPartitionRoot: true` | source | `FilterIgnoredWithoutPartitionRoot` | Without it, each leaf is published under its own, empty filter and column list, so every row and column would flow, silently |

A table's **replica-identity columns** are the *key* columns of the index
concerned. A covering index — `PRIMARY KEY (id) INCLUDE (region)` — carries its
non-key columns in `pg_index.indkey` too, and counting those would let a filter
on `region` past the gate; the source would then refuse the table's `UPDATE`s
and `DELETE`s, which is the failure the gate exists to prevent. Only the first
`indnkeyatts` entries count, on both sides.

`ColumnTypeMismatch` is reported as a **warning only**. The table is still
published, because apply converts values through their text form and a
deliberate widening such as `int` → `bigint` is common.

#### When an eligible table later stops qualifying

What happens depends on which side changed.

- **Source-side failure** (for example, a primary key was dropped). The table is
  **removed from the publication** on the pass that notices. Removing it is the
  only way to let the source application's writes succeed again. The table
  stays listed as excluded, and it comes back once the check passes. See D6.
- **Target-side failure** (for example, a column was dropped on the target). The
  table **stays published**. Removing it would not help: the change that broke
  apply is already in the stream. It is reported in `status.tableWarnings`,
  marked as kept published, and once the subscription exists the operator
  names it as the suspect in a stall.

### Row filters are structured, never SQL text

Predicates are composed with `sql.Identifier` for columns and `sql.Literal` for
values. They are never accepted as raw SQL text; see D2.

Steady state still has to be a no-op, but the server re-deparses the filter
(`pg_publication_tables.rowfilter`), so its text never matches what the
operator sent. The operator therefore records, per table, a hash of the filter
spec and the server's deparsed text after applying it. A filter is re-applied
only when one of those changes, and a change in the server's text means
someone edited it out of band. With nothing recorded (status lost) a filter is
re-applied once.

`status.rowFilters` is a list, not an object keyed by table. Status is written
as a JSON merge patch, so a key removed from a status *object* would linger,
while a list is replaced whole.

Column lists work the same way: the operator compares them against
`pg_publication_tables.attnames`.

### The subscription

- **Owner.** The master user owns the subscription. `run_as_owner = false`, so
  changes apply as each table's owner (normally `<db>_owner`), never as a role
  that table owners could subvert.
- **Connection string.** It is built with `psycopg.conninfo.make_conninfo` and
  passed as a single `sql.Literal`. It includes `connect_timeout=10`, so an
  unroutable source fails `CREATE SUBSCRIPTION` quickly instead of riding the
  statement timeout.
- **Redaction.** Every statement that carries the connection string is executed
  with `sql.execute(..., describe=…)` and a redacted rendering. The password
  therefore reaches neither operator logs nor status messages.
- **Server logs.** The password does end up in the target server's statement log
  if `log_statement` is `ddl` or `all`. That is documented as an operating
  caveat.
- **Connection fingerprint.** `subconninfo` cannot be read back. Status keeps a
  hash of host, port, user, sslmode and password instead, using the same
  material as `Endpoint.fingerprint`. `ALTER SUBSCRIPTION … CONNECTION` runs
  only when the hash changes.
- **Two different "unreachable"s.**
  - `CREATE SUBSCRIPTION` failing with "could not connect to the publisher"
    means the target cannot reach the source. It is reported as
    `SourceUnreachableFromTarget`, and the remedy points at security groups,
    routing and `source.connection.host`.
  - The operator failing to reach either server is the ordinary `Reachable`
    condition.
  - A source that is reached but refuses the login a replication connection
    is `ReplicationRefused`. On RDS the login lacks `rds_replication`; on a
    stock server it lacks the `REPLICATION` attribute.

### Steady state

On a converged replication, a pass runs only catalog reads and statistics reads
on both servers. No DDL is issued, and the only status that changes is
`status.health` (see D4). The existing no-op assertion is extended to this kind
on that basis.

## Health

On every pass, the operator reads:

| Signal | Query | Side |
| --- | --- | --- |
| Lag (bytes not yet confirmed by the target) | `pg_current_wal_lsn() - confirmed_flush_lsn` from `pg_replication_slots` | source |
| Retained WAL | `pg_current_wal_lsn() - restart_lsn` | source |
| Slot active, `wal_status`, `safe_wal_size` | `pg_replication_slots` | source |
| Apply worker running, last message received | `pg_stat_subscription` (`pid`, `last_msg_receipt_time`) | target |
| Apply and sync error counts | `pg_stat_subscription_stats`. On PostgreSQL 18, also the `confl_*` conflict counters | target |
| Per-table state | `pg_subscription_rel.srsubstate`: `i` `d` `f` `s` `r` | target |

The results go to `status.health`, and `status.replicationState` is derived
from them:

| State | When |
| --- | --- |
| `Initializing` | Subscription exists and some published table is not yet `r` |
| `Streaming` | All tables `r`, worker running, slot active, under every threshold |
| `Lagging` | Lag over `maxLag`, or retained WAL over `maxRetainedWal`. It clears at 80% of the threshold (hysteresis), so the state does not flap |
| `Stalled` | Enabled but the worker is not running. Or: the slot has had no consumer for longer than `maxSlotInactive`, or the error counters rose since the last pass |
| `SlotLost` | `wal_status = lost`: `max_slot_wal_keep_size` invalidated the slot. Recovery means deleting and recreating the resource, which runs a full re-copy |
| `Disabled` | `spec.enabled: false` |

**Precedence, worst first:** `SlotLost`, `Disabled`, `Stalled`, `Lagging`,
`Initializing`, `Streaming`. `SlotLost` outranks even `Disabled`, because a
switched-off subscription must not hide that its replica now needs rebuilding.
A slot missing altogether counts as lost.

A new `Streaming` condition carries the state:

- **`True`** for `Streaming` and `Initializing`.
- **`False`** for the other four.
- Only `Lagging`, `Stalled` and `SlotLost` make the phase **`Degraded`** (see
  D3). It outranks `Drifted`.
- `Disabled` is `False`, but a subscription switched off on purpose is healthy,
  so the phase stays `Ready`.

The condition's message carries no live numbers. Those go to `status.health`,
so the condition changes on a transition rather than on every pass (D4).

**A pass that fails is still observed.** Suppose a replication's `PostgresDB`
was deleted under `RETAIN`. The pass can no longer resolve, but its slot still
pins WAL. After any failed pass, the handler reads the slot and subscription
from the names and oid recorded in status, and writes `status.health`,
`replicationState` and the metrics. It writes nothing when the source cannot be
read: reporting a slot lost because it could not be seen would be worse than
silence.

The slot's inactive time is tracked by the operator itself, from the first
pass that saw the slot without a consumer. `inactive_since` only exists from
PostgreSQL 17.

**PostgreSQL does not expose apply-error text over SQL.** It exposes error
*counts* only. The message itself goes to the target server's log (CloudWatch
on RDS). The `Stalled` message says so. When eligibility finds a likely cause,
such as a column added on the source but not the target, the message names it.

## Teardown

Teardown is the same regardless of `retentionPolicy`, which governs real data,
not plumbing. It works from the names and instances **recorded in status**, not
from the `dbRef`s, because those `PostgresDB`s may already be gone. That is
always true when a whole namespace is being deleted.

1. **Target, subscription:**
   1. `ALTER SUBSCRIPTION … DISABLE`.
   2. `DROP SUBSCRIPTION`. Through the target's own connection to the
      source, this also drops the slot and any table-sync slots.
   3. If that fails, because the target cannot reach the source or a walsender
      still holds the slot, the subscription is detached (`SET (slot_name =
      NONE)`) and dropped anyway. That is safe because the slot's name and the
      subscription's oid are recorded in status: step 2 finds the slot, and
      the table-sync slots by their `pg_<oid>_sync_*` names.
2. **Source, slots:**
   1. Any slot still held has its walsender ended (`active_pid`). Its
      subscriber is gone or detached.
   2. The slots are dropped by the managing role where it holds the
      replication privilege (on RDS, through `rds_replication`). Otherwise the
      replication login drops them, since it was created to use exactly these
      slots. For a generated login, teardown first sets a fresh password and
      does not trust the Secret's. A password changed out of band would
      otherwise leave the slot undroppable by anyone.
   3. **The finalizer is held until no slot remains.**
3. **Source, publication:** `DROP PUBLICATION`, if `status.created` says the
   operator made it.
4. **Source, login:** revoke `<db>_ro` and `rds_replication`, terminate the
   login's sessions, then `DROP ROLE`. This happens for a generated login only.
   A bring-your-own login loses only the memberships the operator granted.

   `status.created.login` and `.secret` say which it is, and a pass that adopts
   a provided login writes them **false** rather than leaving them out — status
   is a JSON merge patch, so a key dropped here would keep an earlier pass's
   `true` and teardown would drop a role and delete a Secret the operator never
   made. Adding, removing or repointing `credentialsSecretRef` on a live
   replication swaps one login for another: the one being replaced is removed
   on that same pass, after the subscription carries the new connection string,
   and only as far as it is the operator's — a generated login and its Secret
   go, a provided one keeps both and loses only the memberships the operator
   granted it.
5. **Secret:** delete the credentials Secret, for a generated login only, and
   only once the source is cleared. It is the login's credential, and step 2
   may still need it.
6. **Target data:** never touched.

Each server is handled independently. If the **target is unreachable**, the
source is cleared anyway, because the slot is the dangerous part. The
subscription is then retried as a temporary error, and when it can be reached
it is dropped detached. If the **source is unreachable**, the subscription is
still dropped; the target can usually reach the source even when the operator
cannot. The source is then retried until its slots are confirmed gone.

`run_teardown`'s `InstanceNotFound` shortcut, which releases the finalizer, is
wrong for a resource with two sides. If the target `PostgresInstance` is gone,
the source slot must still be dropped. The replication teardown therefore
handles each side's instance separately. It releases the finalizer only when
both sides are done, or when both sides' instances are gone.

## Interaction with the existing kinds

- **`PostgresInstance`.** Status gains a `logicalReplication` block: `walLevel`,
  `maxReplicationSlots`, slots in use, `maxWalSenders`,
  `maxSlotWalKeepSize`, `canCreateSubscription`, and `replicationGrant`
  (`rds_replication` | `none`). The block is informational: an instance that
  has no replications never warns because of it. The dashboard shows it on the
  instance page. `server.py`'s `describe_server` grows the probes.
- **`PostgresDB` with `DROP`.** Its teardown waits, with a temporary error that
  names the `PostgresReplication`, while any replication references the
  database as source or target. Without the wait:
  - on the target, `DROP DATABASE` fails with a raw "being used by logical
    replication subscription" error;
  - on the source, `FORCE` terminates the walsender and drops the slot along
    with the database, and the replication breaks silently.

  See D7.
- **`PostgresDB` with `RETAIN`.** Deleting it touches nothing. The replication
  then reports `ReferenceNotFound`. The health observation keeps running from
  the names recorded in status, so the slot stays monitored.
- **`PostgresUser`.** No interaction. Users granted on the target read
  replicated tables through the normal `RO`/`RW` groups.
- **README retention table.** Gains a row: "`PostgresReplication` deleted —
  always: subscription, slot, publication and replication login removed;
  replicated data kept."

## Surfaces

### kubectl

```
$ kubectl get postgresreplications -A
NAMESPACE  NAME                 SOURCE           TARGET                        TABLES  LAG     STATE      PHASE  AGE
team-a     orders-to-reporting  prod-rds/orders  reporting-rds/orders_replica  40/41   12 KiB  Streaming  Ready  9d
```

### Metrics

Every series carries `{namespace,name}`. Series are removed on teardown, as
`_forget_resource_metrics` already does.

```
pg_operator_replication_lag_bytes
pg_operator_replication_retained_wal_bytes
pg_operator_replication_slot_active                       0/1
pg_operator_replication_worker_running                    0/1
pg_operator_replication_tables{state}                     published|ready|syncing|excluded
pg_operator_replication_errors{kind}                      apply|sync (gauge: PostgreSQL stats can reset)
pg_operator_replication_observed_timestamp_seconds        so stale data is alertable
```

The existing `pg_operator_reconcile_*` and `pg_operator_resource_ready` series
pick up the new kind automatically.

The chart gains `metrics.prometheusRule.enabled` (default off). It ships alerts
for lag, retained WAL, an inactive slot, rising errors, `SlotLost`, and stale
observation.

### State API

| Endpoint | Returns |
| --- | --- |
| `GET /api/v1/replications` | `?namespace=` `?instance=` (either side) `?database=` `?phase=` `?state=` |
| `GET /api/v1/replications/{ns}/{name}` | One replication, including per-table state and exclusions |

- **Instance and database detail.** `InstanceDetail` gains `replication_list`
  and `DatabaseDetail` gains `replications`. Each is one list, not split into
  outbound and inbound: every entry names its source and target, which says
  the direction already.
- **Overview.** `/overview` gains replication phase counts. Its `attention` list
  gains `Degraded` replications and excluded tables.
- **Stable projection.** The response model is a projection, like the other
  kinds.
- **No credentials.** It names the credentials Secret and its keys, never their
  values, and never a connection string. The existing test that asserts no
  endpoint emits a password or URI covers the new endpoints.

### Dashboard

- **Replications list.** Shows source → target, state, lag, retained WAL and
  tables ready/total. Filters live in the URL, matching the API parameters.
- **Replication detail.** Shows lag and retained WAL against their thresholds,
  and a table-by-table grid. Each row shows its sync state, or why it is
  excluded, with the remedy. The page also lists the objects (publication,
  subscription, slot, login), the Secret by name only, and the raw conditions.
- **Links from existing pages.** Instance and database pages gain inbound and
  outbound replication panels. Overview gains a replications card. The nav
  gains an entry.
- **Same constraints as the rest of the UI.** Plain JS on Mantine 9, with no new
  dependencies.

## Testing

### Unit

- **Models.** Spec parsing and defaults, plus Python mirrors of the CEL rules.
- **Naming.** The 63-byte limit, the hash suffix, and keeping `rds*` and `pg_`
  prefixes out.
- **Row filters.** DSL → `sql.Composed`, asserted on the rendered SQL.
  Identifiers stay quoted and values stay literal, including hostile inputs
  such as `region'); DROP …`.
- **Eligibility.** A pure function over catalog rows, so every reason in the
  table is tested without a server.
- **State and phase.** State, phase and hysteresis derivation.
- **Teardown.** The state machine against `tests/fakes.py`, including the
  one-side-gone paths.
- **API and CRD.** API projections; `test_crds.py` and `test_examples.py` for
  the new CRD and `examples/04-replication.yaml`.

### Integration

Replication needs two servers: one server subscribing to itself hangs unless
the slot is created by hand. `make test-setup` grows a second container. Both
containers run `wal_level=logical`, sit on a shared Docker network, and are
bootstrapped RDS-like as today. The bootstrap also runs:

- `GRANT pg_create_subscription TO rds_superuser` (16+);
- `GRANT pg_monitor TO rds_superuser`;
- `CREATE ROLE rds_replication NOLOGIN`, granted to `rds_superuser WITH ADMIN`.

New environment variables:

- `PGOP_TEST_TARGET_DSN` points at the target.
- `PGOP_TEST_SOURCE_FROM_TARGET` holds the source's address *as the target
  container sees it*. This exercises `source.connection.host` for real.
- `PGOP_TEST_SUPERUSER_DSN` is used by exactly one fixture,
  `emulate_rds_replication()`. It runs `hack/rds-replication-shim.sql`, which
  sets `REPLICATION` on logins that are *direct* members of `rds_replication`.
  It emulates the RDS walsender rule and nothing else (see D8). Test cleanup
  also uses it, so that a failed test cannot strand a slot. Every other
  statement runs as `pgadmin`.

Already proved by PR 1's harness tests, as `pgadmin`, on 16→16, 17→17, 18→18,
16→18 and 18→16:

- **Grant without leakage.** `GRANT rds_replication` succeeds and reads back,
  and the managing role does not gain `REPLICATION`.
- **Subscriptions.** `CREATE SUBSCRIPTION` works through `pg_create_subscription`,
  and inserts, updates and deletes all reach the target.
- **Self-heal path.** A new password set with `ALTER ROLE` and pushed with
  `ALTER SUBSCRIPTION … CONNECTION` resumes streaming. The managing role may
  change the password of a login that carries the `REPLICATION` attribute.
- **Normal teardown.** `DROP SUBSCRIPTION` removes the slot on the source. The
  managing role can then drop the publication and the login.
- **Fallback teardown.** `SET (slot_name = NONE)` leaves a detached slot, and
  the replication login can drop it.

Scenarios:

- **End-to-end flow.** Create; wait for the initial copy; `INSERT`, `UPDATE`,
  `DELETE` and `TRUNCATE` on the source show up on the target. A row filter and
  a column list are applied and read back.
- **Steady state.** The second pass issues no DDL.
- **Missing target table.** A table created only on the source is excluded and
  other tables keep streaming. Once the table is created on the target, it is
  published and copied on the next pass.
- **No replica identity.** A table without one is excluded, **and the test
  asserts that `UPDATE` on the source still succeeds**.
- **Replica identity lost.** Drop a primary key on a published table. The next
  pass removes the table from the publication, and source writes recover.
- **Target column dropped.** The pass reports `Stalled`, naming the table.
- **Credentials Secret deleted.** The password is regenerated, the connection is
  re-pointed, and streaming resumes.
- **Bring-your-own login.** Pre-created by the superuser fixture.
- **Teardown, both sides reachable.** No slot, publication or login remains, and
  the target data is intact.
- **Teardown, target container stopped.** The source slot is still dropped and
  the finalizer waits for the target.
- **Teardown, source stopped.** The finalizer holds, and no slot is orphaned.
- **`PostgresDB` `DROP` teardown.** It waits for a replication that references
  it.

### CI

- **Version matrix.** Replication tests skip on 14 and 15, with the reason
  given. They run in a new matrix of source → target pairs: 16→16, 17→17,
  18→18, 16→18 and 18→16.
- **How the servers start.** GitHub service containers cannot take server
  arguments such as `wal_level`, so this job starts its servers with `docker
  run`, through the same script as `make test-setup`.

### Local cluster harness

- **Server changes.** `pg16`, `pg17` and `pg18` gain `-c wal_level=logical`
  and the bootstrap grants above.
- **Emulation shim.** A small test-only sidecar per server runs a psql loop as
  the container superuser. It applies the same `REPLICATION` emulation, so the
  **generated-login** path runs end to end in-cluster.
- **Workload.** `03-replication.yaml` adds three replications: pg16→pg17,
  pg17→pg18 and pg18→pg16.
- **Make targets.** `local-verify` asserts that rows flow. `local-status` lists
  the new kind.
- **What only a live cluster proves.** The new CEL rules, RBAC for the kind,
  the print columns, and timer behaviour on a fourth kind.

### Real-RDS verification gate

The harness cannot prove the rows marked **?** in the privilege table. Before
release, one RDS 16 or 17 pair must confirm each of the following:

- **Subscriptions.** The master user can `CREATE`, `ALTER` and `DROP
  SUBSCRIPTION`. Find out whether `pg_create_subscription` is involved or this
  is a patched check.
- **Streaming.** A login holding only `rds_replication` (no `REPLICATION`
  attribute) can open a subscription's walsender connection.
- **Slots.** The master user can `pg_drop_replication_slot` through
  `rds_replication`.
- **Publications.** `ALTER PUBLICATION … ADD TABLE` works for tables owned by
  `<db>_owner`.
- **Connection string.** `subconninfo` is unreadable, as assumed.
- **TLS.** Whether any server-local CA path works for `sslmode=verify-full` from
  an RDS subscriber (D11).
- **Statement log.** What `log_statement=ddl` puts in CloudWatch for `CREATE
  SUBSCRIPTION`.
- **Multi-AZ failover.** What happens to the slot when a Multi-AZ DB instance
  fails over (undocumented).

## Delivery plan

Each PR updates `README.md` for what it ships.

| PR | Scope | Exit criteria |
| --- | --- | --- |
| 1 | Prerequisites: the instance's `logicalReplication` status block; harness and CI changes (second server, `wal_level`, bootstrap grants, shim) | Capabilities reported correctly on 14–18; existing suites green, unchanged |
| 2 | CRD, models, naming, row-filter DSL, chart (CRD, RBAC, aggregation), example. Handlers registered; step 1 (Resolve) implemented in full, including the cycle check; status `Pending` via `Ready=Unknown` | `test_crds`/`test_examples`, spec and reconcile unit tests; CEL rules verified with `make local-up` |
| 3 | Source side: login, Secret, table resolution, eligibility, publication convergence, and the teardown of all of it. Teardown was planned for PR 4, but PR 3 is the first PR to *create* objects, so shipping removal later would strand them | Integration: publication contents and exclusions, steady-state no-op, teardown (including after a failed pass) proven against real servers |
| 4 | Target side: subscription, refresh, connection fingerprint, enable/disable; the slot and subscription half of teardown; `PostgresDB` teardown ordering; `TargetTableNotEmpty` | Integration: rows stream through filters and column lists; steady-state no-op; teardown with both servers, with either unreachable, and leaving no table-sync slots |
| 5 | Health: observation (also after a failed pass), `status.health`, `Streaming` condition, `Degraded` phase, a 60s `reconcile.replicationInterval` timer, metrics, `PrometheusRule` | Integration: Lagging, Stalled (broken login; real apply errors with the suspect named), SlotLost (a real `max_slot_wal_keep_size` invalidation), Disabled, observation without resolution |
| 6 | State API: replications list/detail, replications on instance and database detail, the instance's readiness block, `Degraded`, overview counts and attention; the replication CRD optional on `/readyz` | Projection and filter tests; credential-leak test extended to every replication endpoint |
| 7 | Dashboard: Replications list and detail (lag and retained WAL as meters against their thresholds; a per-table grid), replication panels on instance and database pages, the instance's readiness, an overview card, the nav entry | `make ui-check`, `make ui-image-check`, exercised on `make local-ui` |
| — | Real-RDS gate | Every item above confirmed or the design adjusted. `CLAUDE.md` updated: four CRDs, the two-server harness |

PRs 6 and 7 can proceed alongside 3–5 against fixture status objects.

## Decisions for review

These are calls this document made that the Q&A did not cover. **All
eleven were accepted on 2026-10-21**, as recommended below.

| # | Decision | Recommendation and reasoning |
| --- | --- | --- |
| D1 | Explicit `FOR TABLE` lists instead of `FOR TABLES IN SCHEMA` | Adopt. It is the only way to stop a source-first migration from stalling every table. It also needs only table ownership, which works identically on RDS and in the harness |
| D2 | Structured row filters instead of a raw `WHERE` string | Adopt. A raw string would put user-authored SQL text into DDL run as the master user, breaking the "identifiers never reach SQL as text" invariant. Validating text cannot rule out statement-level injection, such as closing the parenthesis to publish another table. The DSL covers the predicates replication filters realistically need. Raw expressions could follow later behind an explicit opt-in |
| D3 | A new `Degraded` phase and `Streaming` condition, rather than reusing `Drifted` | Adopt. `Drifted` is documented as spec-versus-live divergence with a fixed table of reasons. Lag is a health measurement, and folding it in would make `Drifted` flap with load |
| D4 | `status.health` is exempt from the no-status-churn rule; replications get their own timer, `reconcile.replicationInterval`, default 60s | Adopt. Lag is inherently time-varying, and one status patch per replication per minute is negligible. Conditions and phase still change only on transitions, with hysteresis. kopf's update handlers ignore status, so this cannot loop |
| D5 | The generated credentials Secret lives in the operator namespace, not the resource's | Adopt. No application consumes it, so keeping it out of team namespaces narrows exposure |
| D6 | A table whose replica identity is lost on the source is removed from the publication automatically | Adopt. Source writes are already failing, and removal is the only fix within the operator's reach. Replica completeness gives way to source availability |
| D7 | A `PostgresDB` `DROP` teardown waits for replications that reference it | Adopt. Otherwise the failure is either a raw SQL error on the target, or a silently killed slot on the source |
| D8 | The harness emulates RDS's `rds_replication` walsender rule with one superuser step | Adopt. It is the same pattern as the existing `GRANT SET ON PARAMETER` emulation. Everything else stays non-superuser |
| D9 | Support a bring-your-own replication login (`credentialsSecretRef`) | Adopt. It covers non-RDS servers where the operator cannot grant replication |
| D10 | Reject replication within one server | Adopt for v1. The slot is now always created separately with `create_slot = false` (step 6), which removes the main obstacle; same-server support is a candidate follow-up |
| D11 | Default `sslMode: require`, with `verify-*` only through an explicit server-local `sslRootCertPath` | Adopt, pending the RDS gate. AWS documents no usable CA path on an RDS subscriber |

## Operating guidance

This goes into the README alongside the resource reference.

- **Schema changes run against the target first** when they are additive
  (a new table, a new column), and against **the source first** when they
  remove something. A column the source sends and the target lacks stops apply.
  A column the target has and the source lacks is harmless. Renames need two
  steps: add, backfill, then drop.
- **Set `max_slot_wal_keep_size`** in the source's parameter group. It is the
  hard backstop that the operator deliberately is not, and without it a dead
  subscriber fills the disk.
- **Network.** The target's security group must allow egress to the source, and
  the source's must allow ingress from the target. Set
  `source.connection.host` if the target resolves the source differently from
  the operator.
- **Major-version upgrades and blue/green.** At the time of writing, AWS's
  documentation says RDS's major-version upgrade precheck refuses an instance
  that has logical slots. Slots are preserved only when upgrading from 17. The
  blue environment of a blue/green deployment cannot be a logical publisher or
  subscriber. Delete the replication first and expect a full re-copy afterwards.
- **Sequences are not replicated.** For a read replica this does not matter. It
  matters if the replica is ever promoted, which is a non-goal here.
- **`TRUNCATE` replicates.** On the target, it fails if a non-replicated table
  references the replicated one by foreign key. Drop `truncate` from `publish`
  in that case.
- **The replication password** is stored in the target's catalog. It also
  appears in the target's statement log if `log_statement` is `ddl` or `all`.
  The login can read only what `<db>_ro` can read.

## Follow-ups

- PostgreSQL 17 failover slots (`failover = true`) for Multi-AZ DB clusters and
  read-replica standbys.
- An explicit, auditable re-sync trigger, used after `SlotLost` or to re-copy one
  table.
- Replication within one server (D10).
- An opt-in raw row-filter expression (D2).
- Migration and cutover, which would bring sequence sync and promotion.

## References

- PostgreSQL 16: [CREATE PUBLICATION](https://www.postgresql.org/docs/16/sql-createpublication.html), [CREATE SUBSCRIPTION](https://www.postgresql.org/docs/16/sql-createsubscription.html), [ALTER SUBSCRIPTION](https://www.postgresql.org/docs/16/sql-altersubscription.html), [DROP SUBSCRIPTION](https://www.postgresql.org/docs/16/sql-dropsubscription.html), [logical replication security](https://www.postgresql.org/docs/16/logical-replication-security.html), [role attributes](https://www.postgresql.org/docs/16/role-attributes.html)
- AWS: [logical replication on RDS for PostgreSQL](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/PostgreSQL.Concepts.General.FeatureSupport.LogicalReplication.html), [rds_superuser](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/Appendix.PostgreSQL.CommonDBATasks.Roles.rds_superuser.html), [predefined roles](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/Appendix.PostgreSQL.CommonDBATasks.Roles.View.html), [major-version upgrades](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/USER_UpgradeDBInstance.PostgreSQL.MajorVersion.Process.html), [blue/green considerations](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/blue-green-deployments-considerations.html), [logical slot synchronisation](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/Appendix.PostgreSQL.CommonDBATasks.pglogical.slot.synchronization.html)
- AWS blogs: [RDS to self-managed logical replication](https://aws.amazon.com/blogs/database/using-logical-replication-to-replicate-managed-amazon-rds-for-postgresql-and-amazon-aurora-to-self-managed-postgresql/), [Aurora cross-version with native logical replication](https://aws.amazon.com/blogs/database/migrate-amazon-aurora-postgresql-across-major-versions-with-active-debezium-cdc-connectors-using-native-logical-replication/)
