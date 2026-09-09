# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Kubernetes operator that manages databases, roles and grants on **external**
PostgreSQL servers (typically Amazon RDS) — it does not run PostgreSQL itself.
Three CRDs: `PostgresInstance` (cluster-scoped: a server + superuser
credentials), `PostgresDB` (namespaced: a database, its schemas/extensions/group
roles), `PostgresUser` (namespaced: a login role and its access). The
reconciler, a read-only FastAPI state API, and a React dashboard run as three
containers in one pod. Full architecture, the access model, and every CRD
field are documented in `README.md` — read it before making non-trivial
changes; this file only covers what you need to build, test, and navigate.

## Commands

```bash
uv sync --extra dev                  # install (uv only — see below)

uv run ruff check src tests          # lint
uv run mypy src/pg_operator          # types
uv run pytest                        # unit tests (integration tests skip without PGOP_TEST_DSN)
uv run pytest tests/test_roles.py -k some_test   # a single test
helm lint charts/pg-operator         # chart

make ui-install                      # dashboard's npm deps
make ui-check                        # ESLint + production build for ui/

make check                           # everything CI runs (lint, typecheck, test, chart-lint, ui-check)
```

Never use `pip` in this repo — always `uv` (`uv sync`, `uv add`, `uv run ...`),
even if dependency resolution fails; work through the failure with uv rather
than falling back to pip.

### Integration tests: must run as a non-superuser

The integration suite (`tests/integration/`) is where the privilege model is
actually proved, and it's how most RDS-specific bugs were found. It needs a
live PostgreSQL reachable via `PGOP_TEST_DSN`, connected as a role that mirrors
the RDS master user: a member of `rds_superuser` with `CREATEDB`/`CREATEROLE`,
**not** a true superuser. Testing as an actual superuser (e.g. the `postgres`
role) hides an entire class of bugs — missing `INHERIT`/`SET` grants, silent
`GRANT`/`REVOKE` warnings, `DROP DATABASE ... WITH (FORCE)` failures — that only
surface under RDS's real constraint. Always test as a non-superuser, matching
production.

```bash
make test-setup        # starts postgres:16-alpine with an RDS-like managing role
make test-integration   # runs the full suite against it
make test-teardown
```

CI runs the matrix against PostgreSQL 14/15/16/17/18 — they take different
code paths (per-grant `INHERIT`/`SET` options arrive in 16, `pg_database_owner`
ownership of `public` in 15, `GRANT ... ON PARAMETER` in 15).

### The local cluster harness (`hack/local/`)

Runs the operator in a real Kubernetes cluster (tested on OrbStack) against
five real PostgreSQL servers, 14 through 18, each bootstrapped with an
RDS-like non-superuser managing role.

```bash
make local-up       # build both images, start the servers, install the operator
make local-status   # phases for all three kinds
make local-verify   # connect as each generated user, assert privileges
make local-test     # full pytest suite against each server version
make local-ui       # port-forward the dashboard to localhost:8081
make local-api      # port-forward the API to localhost:8000 (pair with `make ui-dev`)
make local-down
```

**Some bugs only show up on a live cluster** — things the fake Kubernetes API
in `tests/fakes.py` and the unit/integration suites cannot catch: real kopf
timer/retry scheduling, real CRD admission (CEL validation rules, defaulting),
RBAC actually denying an API call, multi-container pod networking (the UI's
nginx proxy to the API sidecar), and real status/print-column rendering via
`kubectl get`. When a change touches CRD schemas, RBAC, kopf handler
registration/timers, or the pod's container topology, verify it with
`make local-up` rather than trusting unit tests alone.

### Working on the dashboard (`ui/`)

Plain JavaScript (no TypeScript), React on Mantine 9, themed from Tailwind's
design tokens folded into Mantine's palette — no Tailwind dependency, no
webfont, no external requests from the page at all. There is deliberately no
test runner in `ui/`; correctness is enforced by ESLint, the production build,
and `make ui-image-check` (see below). Don't add one unless asked.

```bash
make local-api    # shell 1: port-forward the real API to localhost:8000
make ui-dev       # shell 2: http://localhost:5173, hot reload, proxies like nginx does
```

`make ui-image-check` builds the dashboard image and runs it under the pod's
actual constraints (`--read-only --user 65532:65532`), asserting nginx comes
up, the SPA fallback serves deep links, a missing asset still 404s, and `/api`
proxies correctly — the one thing about this container that can't be verified
any other way.

## Architecture

### Reconciliation model

Every reconcile — create, update, resume, or the periodic timer — runs the
*same* convergence code: make the live server match the spec, full stop. There
is no separate "initial create" path. Steady state must be a genuine no-op (no
statements, no status churn), which is asserted directly in tests.

`retentionPolicy` (`RETAIN` default, or `DROP`) governs whether removing
something from spec actually deletes it in Postgres; only objects the operator
itself created (tracked in `status.managedSchemas`/`status.managedExtensions`)
are ever eligible for deletion. See README's "Retention" section before
touching any teardown path.

### Source layout (`src/pg_operator/`)

```
models.py          typed CR specs; defaults and derivations live here
naming.py          Kubernetes name -> PostgreSQL identifier
status.py          metav1.Condition helpers
postgres/
  connection.py    fingerprint-keyed connection pools; a rotated Secret retires the pool
  sql.py           SQL composition helpers — nothing is ever string-interpolated
  server.py        version and effective-privilege detection
  roles.py         group roles, login roles, membership, teardown
  database.py      databases, schemas, extensions, parameters
  privileges.py    the OWNER/RW/RO grant matrix
  settings.py      pg_db_role_setting parsing
k8s/               client, cross-namespace Secrets, PushSecret, CR access
reconcile/         instance/database/user convergence + teardown (context.py holds
                   the shared ReconcileContext: instance resolution, connections, events)
handlers/          kopf registration; every handler funnels through
                   handlers/common.py's run_reconcile for uniform retry
                   mapping, status writes and metrics
api/               read-only FastAPI state API — projects CR status, holds no
                   state of its own, never touches PostgreSQL directly
```

Two error types drive retry behaviour everywhere (`errors.py`):
misconfiguration is **permanent** (retrying can't help until the spec
changes), while an unreachable server or a failed statement is **temporary**
(retried with backoff). Both are written to resource status, not just logged.

### Security-relevant invariants

- **Identifiers never reach SQL as text.** Every identifier goes through
  `psycopg.sql.Identifier`; every value is a bound parameter or `sql.Literal`.
  Names are validated at CRD admission and again in `models.py`. Any new SQL
  composition must go through `postgres/sql.py`'s helpers, not raw f-strings.
- **RDS's master user is not a superuser** (member of `rds_superuser`, with
  `CREATEDB`/`CREATEROLE` only). This shapes real code paths — see README's
  "RDS specifics" section for the concrete consequences (role `SET`/`INHERIT`
  grants, `ALTER ROLE` no-op behaviour, `DROP OWNED BY`, `DROP DATABASE ...
  FORCE`, unreadable `pg_authid`). Any change touching roles, ownership, or
  attribute reconciliation needs to be reasoned about under this constraint,
  and verified against a non-superuser test role, not a real superuser.
- **PostgreSQL warns rather than errors** on an under-privileged `GRANT`/
  `REVOKE`. Every connection installs a notice handler that logs server
  warnings, and `status.schemaGrants` reports privileges read back from the
  server — never assume a `GRANT` succeeded just because it didn't raise.

### Deliberate omissions

README's "Design decisions" section records choices that look like gaps but
aren't — most notably: no scheduled password rotation (the operator self-heals
a deleted credentials Secret instead; real rotation needs an explicit,
auditable trigger and is the next increment), generated Secrets carry no owner
reference (owner refs can't cross namespaces, and `RETAIN` must leave a working
credential behind), and the API reads resource status rather than querying
PostgreSQL. Check there before "fixing" something that appears missing.
