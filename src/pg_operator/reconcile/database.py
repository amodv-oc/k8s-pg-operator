"""PostgresDB reconciliation.

Reconciliation is a full convergence pass, not a create-once step: every
reconcile re-asserts the database's owner, its schemas, its extensions, its
parameters, and the whole grant matrix. Adding a schema to an existing
PostgresDB therefore creates it and wires up all three group roles on the next
pass, with no separate migration path.

Two facts shape the ordering:

* Roles are cluster-wide and ``CREATE DATABASE`` cannot run inside a
  transaction, so group roles and the database itself are handled on the
  maintenance connection first.
* Schema, extension and grant work has to happen *inside* the target database,
  on a second connection.

What the operator has previously created is recorded in
``status.managedSchemas`` / ``status.managedExtensions``. Orphan detection
compares that record against the spec, so an object created by a DBA outside the
operator is reported as unmanaged and never dropped, even under a DROP policy.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from psycopg import AsyncConnection, sql

from .. import status as st
from ..constants import DROP, PLURAL_DATABASE
from ..errors import ConfigurationError, ReconcileFailure
from ..models import DatabaseSpec, RetentionPolicy, effective_retention, parse_database_spec
from ..postgres import database as db
from ..postgres import roles as pgroles
from ..postgres.privileges import (
    GrantOptions,
    GroupRoles,
    apply_default_privileges,
    apply_schema_privileges,
    observed_schema_grants,
)
from ..postgres.sql import fetch_scalar
from .context import ReconcileContext, ResolvedInstance

log = logging.getLogger(__name__)

#: Installed in every database from template1; never treated as drift.
BASELINE_EXTENSIONS = frozenset({"plpgsql"})


def _normalise_encoding(value: str) -> str:
    """``UTF-8``, ``utf8`` and ``UTF8`` all name the same server encoding."""
    return value.upper().replace("-", "").replace("_", "")


@dataclass(slots=True)
class DatabaseResult:
    """Outcome of one database reconcile."""

    database: str
    roles: GroupRoles
    created: bool = False
    changes: list[str] = field(default_factory=list)
    orphaned_schemas: list[str] = field(default_factory=list)
    orphaned_extensions: list[str] = field(default_factory=list)
    unmanaged_schemas: list[str] = field(default_factory=list)
    unowned_schemas: list[str] = field(default_factory=list)
    denied_parameters: list[str] = field(default_factory=list)
    immutable_drift: list[str] = field(default_factory=list)
    #: Owner-group members whose default privileges could not be managed when
    #: ``setRoleForOwners`` is off (see ``_owner_creators``).
    unmanageable_creators: list[str] = field(default_factory=list)
    managed_schemas: list[str] = field(default_factory=list)
    managed_extensions: list[str] = field(default_factory=list)
    observed: db.DatabaseObservation = field(default_factory=db.DatabaseObservation)
    schema_grants: dict[str, list[str]] = field(default_factory=dict)
    retention: RetentionPolicy = "RETAIN"

    @property
    def message(self) -> str:
        if self.created:
            return f"created database {self.database}"
        if self.changes:
            return f"converged {self.database}: " + "; ".join(self.changes[:8])
        return f"{self.database} in sync"


async def reconcile_database(
    ctx: ReconcileContext, obj: dict[str, Any]
) -> tuple[DatabaseResult, ResolvedInstance]:
    """Converge one PostgresDB against its instance."""
    meta = obj.get("metadata") or {}
    namespace = str(meta.get("namespace"))
    name = str(meta.get("name"))
    existing_status = obj.get("status") or {}

    spec = _parse_spec(obj)
    instance = await ctx.resolve_instance(
        spec.instance_ref.name, for_namespace=namespace
    )
    database, roles = _derive_names(spec, name, instance.spec.role_prefix)
    retention = effective_retention(spec.retention_policy, instance.spec.retention_policy)

    result = DatabaseResult(database=database, roles=roles, retention=retention)
    options = GrantOptions(
        revoke_public_schema_create=spec.revoke_public_schema_create,
        grant_execute_to_read_only=spec.grant_execute_to_read_only,
        public_schema_locked_by_default=instance.server.public_schema_is_locked_down,
    )

    # -- phase 1: cluster-wide objects, on the maintenance connection --------
    creators: list[str] = []
    async with ctx.connect(instance) as conn:
        await _ensure_group_roles(
            conn,
            roles,
            result,
            per_grant_options=instance.server.supports_per_grant_options,
        )
        await _ensure_database(conn, spec, database, roles, result)
        await _apply_database_privileges(conn, spec, database, roles)
        await _apply_database_attributes(conn, spec, database, result)
        if not spec.set_role_for_owners:
            creators = await _owner_creators(
                conn,
                roles,
                result,
                per_grant_options=instance.server.supports_per_grant_options,
            )

    # -- phase 2: in-database objects ---------------------------------------
    desired_schemas = spec.resolve_schemas()
    async with ctx.connect(instance, database) as conn:
        await _ensure_schemas(conn, desired_schemas, roles, options, result, creators)
        await _ensure_extensions(conn, spec, result)
        await _handle_orphans(
            conn, spec, desired_schemas, existing_status, retention, result
        )
        result.observed = await db.observe(conn, database)
        result.schema_grants = await observed_schema_grants(conn, roles)

    # A retained orphan stays on the record. The operator created it and has
    # only been told not to drop it, so forgetting it would reclassify it as
    # someone else's object on the very next pass: the Drifted condition would
    # clear itself, and switching retentionPolicy to DROP later would no longer
    # remove it. Orphans that were actually dropped are absent from this list,
    # because DROP returns before populating it.
    result.managed_schemas = sorted(
        {s.name for s in desired_schemas} | set(result.orphaned_schemas)
    )
    result.managed_extensions = sorted(
        {e.name for e in spec.extensions} | set(result.orphaned_extensions)
    )
    result.unmanaged_schemas = sorted(
        result.observed.schema_names
        - set(result.managed_schemas)
        - set(result.orphaned_schemas)
    )
    return result, instance


def _parse_spec(obj: dict[str, Any]) -> DatabaseSpec:
    try:
        return parse_database_spec(obj.get("spec") or {})
    except Exception as exc:
        raise ConfigurationError(f"invalid spec: {exc}") from exc


def _derive_names(spec: DatabaseSpec, resource_name: str, prefix: str) -> tuple[str, GroupRoles]:
    """The database name and group role names a PostgresDB resolves to.

    Both derivations can fail - a resource name with no usable characters, a
    role override that collides with a derived name - and both are facts about
    the spec, so they are reported as configuration errors rather than retried.
    """
    try:
        database = spec.resolve_database_name(resource_name)
        roles = GroupRoles.from_mapping(spec.resolve_role_names(database, prefix))
    except ValueError as exc:
        raise ConfigurationError(f"invalid spec: {exc}") from exc
    return database, roles


# ---------------------------------------------------------------------------
# phase 1
# ---------------------------------------------------------------------------


async def _owner_creators(
    conn: AsyncConnection,
    roles: GroupRoles,
    result: DatabaseResult,
    *,
    per_grant_options: bool,
) -> list[str]:
    """Login roles whose future objects need default privileges of their own.

    With ``setRoleForOwners`` on, everything an owner-group member creates
    belongs to the group, so the group's DEFAULT PRIVILEGES cover it. With it
    off each member creates objects under its own name, and PostgreSQL keys
    default privileges on the creating role - so the RW and RO grants have to be
    declared once per member or the readers never see the new tables.

    ``ALTER DEFAULT PRIVILEGES FOR ROLE x`` requires the managing role to be
    able to act as ``x``, which creating ``x`` does not confer; the membership
    is taken here, as teardown does. A member the operator cannot take (one a
    DBA added by hand) is reported rather than allowed to fail the pass.
    """
    current = str(await fetch_scalar(conn, sql.SQL("SELECT current_user")))
    members = sorted(await pgroles.members_of(conn, roles.owner) - {current, *roles.all})
    creators: list[str] = []
    for member in members:
        try:
            await pgroles.ensure_self_membership(
                conn, member, per_grant_options=per_grant_options
            )
        except ReconcileFailure as exc:
            log.warning(
                "cannot manage default privileges for owner-group member %s: %s",
                member,
                exc,
            )
            result.unmanageable_creators.append(member)
            continue
        creators.append(member)
    return creators


async def _ensure_group_roles(
    conn: AsyncConnection,
    roles: GroupRoles,
    result: DatabaseResult,
    *,
    per_grant_options: bool,
) -> None:
    for role in roles.all:
        if await pgroles.ensure_group_role(conn, role):
            result.changes.append(f"created group role {role}")
    # The managing role must be able to both name the owner group as an owner
    # and act as it. On PostgreSQL 16+ creating the role grants neither.
    await pgroles.ensure_self_membership(
        conn, roles.owner, per_grant_options=per_grant_options
    )


async def _ensure_database(
    conn: AsyncConnection,
    spec: DatabaseSpec,
    database: str,
    roles: GroupRoles,
    result: DatabaseResult,
) -> None:
    state = await db.get_database(conn, database)
    if state is None:
        await db.create_database(
            conn,
            database,
            owner=roles.owner,
            encoding=spec.encoding,
            template=spec.template,
            lc_collate=spec.lc_collate,
            lc_ctype=spec.lc_ctype,
            connection_limit=spec.connection_limit,
        )
        result.created = True
        result.changes.append(f"created database {database}")
        return

    # Encoding and locale are fixed at CREATE DATABASE time; PostgreSQL has no
    # ALTER for them. Report the divergence rather than pretending to converge.
    if _normalise_encoding(state.encoding) != _normalise_encoding(spec.encoding):
        result.immutable_drift.append(
            f"encoding is {state.encoding}, spec requests {spec.encoding} "
            "(fixed at creation; requires a dump and reload to change)"
        )
    if spec.lc_collate and state.collate and state.collate != spec.lc_collate:
        result.immutable_drift.append(
            f"lc_collate is {state.collate}, spec requests {spec.lc_collate} "
            "(fixed at creation)"
        )
    if spec.lc_ctype and state.ctype and state.ctype != spec.lc_ctype:
        result.immutable_drift.append(
            f"lc_ctype is {state.ctype}, spec requests {spec.lc_ctype} "
            "(fixed at creation)"
        )

    if state.owner != roles.owner:
        await db.set_database_owner(conn, database, roles.owner)
        result.changes.append(f"reassigned database owner to {roles.owner}")
    if state.connection_limit != spec.connection_limit:
        await db.set_database_connection_limit(conn, database, spec.connection_limit)
        result.changes.append(f"set connection limit to {spec.connection_limit}")


async def _apply_database_privileges(
    conn: AsyncConnection,
    spec: DatabaseSpec,
    database: str,
    roles: GroupRoles,
) -> None:
    if spec.revoke_public_connect:
        await db.revoke_public_database_privileges(conn, database)
    # TEMPORARY lets owner and read-write roles create temp tables, which
    # migrations and bulk loads rely on; read-only deliberately cannot.
    await db.grant_database_connect(
        conn, database, [roles.owner, roles.read_write], with_temp=True
    )
    await db.grant_database_connect(conn, database, [roles.read_only], with_temp=False)


async def _apply_database_attributes(
    conn: AsyncConnection, spec: DatabaseSpec, database: str, result: DatabaseResult
) -> None:
    observed = await db.get_database_parameters(conn, database)
    outcome = await db.apply_database_parameters(conn, database, spec.parameters, observed)
    result.changes.extend(outcome.changes)
    result.denied_parameters.extend(outcome.denied)
    if spec.comment is not None:
        await db.comment_on_database(conn, database, spec.comment)


# ---------------------------------------------------------------------------
# phase 2
# ---------------------------------------------------------------------------


async def _ensure_schemas(
    conn: AsyncConnection,
    desired: list[Any],
    roles: GroupRoles,
    options: GrantOptions,
    result: DatabaseResult,
    creators: list[str] | None = None,
) -> None:
    for schema in desired:
        outcome = await db.ensure_schema(conn, schema.name, roles.owner)
        if outcome is db.SchemaOutcome.CREATED:
            result.changes.append(f"created schema {schema.name}")
        elif outcome is db.SchemaOutcome.OWNER_CHANGED:
            result.changes.append(f"reassigned schema {schema.name} to {roles.owner}")
        elif outcome is db.SchemaOutcome.OWNER_DENIED:
            result.unowned_schemas.append(schema.name)

        if schema.comment is not None and outcome is not db.SchemaOutcome.OWNER_DENIED:
            await db.comment_on_schema(conn, schema.name, schema.comment)

        # Re-applied every pass: this is what makes a newly added schema
        # immediately usable by every existing member of the RW and RO groups.
        # Attempted even for a schema we do not own — the grants no-op with a
        # warning today, and start working the moment ownership is fixed, while
        # status.schemaGrants reports what is actually in force.
        await apply_schema_privileges(conn, schema.name, roles, options)
        await apply_default_privileges(
            conn, schema.name, roles, options, creators=[roles.owner, *(creators or [])]
        )


async def _ensure_extensions(
    conn: AsyncConnection, spec: DatabaseSpec, result: DatabaseResult
) -> None:
    if not spec.extensions:
        return
    installed = {ext.name: ext for ext in await db.list_extensions(conn)}
    available = await db.available_extensions(conn)

    for wanted in spec.extensions:
        if wanted.name not in available:
            result.immutable_drift.append(
                f"extension {wanted.name!r} is not available on this server "
                "(on RDS it must first be added to shared_preload_libraries "
                "or included in rds.allowed_extensions)"
            )
            continue
        current = installed.get(wanted.name)
        if current is None:
            await db.create_extension(
                conn,
                wanted.name,
                schema=wanted.schema_,
                version=wanted.version,
                cascade=wanted.cascade,
            )
            result.changes.append(f"created extension {wanted.name}")
        elif wanted.version and current.version != wanted.version:
            await db.update_extension(conn, wanted.name, wanted.version)
            result.changes.append(
                f"updated extension {wanted.name} {current.version} -> {wanted.version}"
            )


async def _handle_orphans(
    conn: AsyncConnection,
    spec: DatabaseSpec,
    desired_schemas: list[Any],
    existing_status: dict[str, Any],
    retention: RetentionPolicy,
    result: DatabaseResult,
) -> None:
    """Act on objects the operator created that the spec no longer declares.

    Only objects previously recorded in status are eligible: anything else was
    created outside the operator and is reported as unmanaged instead. Under
    RETAIN the orphan is left untouched and surfaced on status; under DROP it is
    removed.
    """
    previously_managed_schemas = {
        str(s) for s in (existing_status.get("managedSchemas") or [])
    }
    previously_managed_extensions = {
        str(e) for e in (existing_status.get("managedExtensions") or [])
    }
    live_schemas = {s.name for s in await db.list_schemas(conn)}
    live_extensions = {e.name for e in await db.list_extensions(conn)}

    desired_schema_names = {s.name for s in desired_schemas}
    desired_extension_names = {e.name for e in spec.extensions}

    orphan_schemas = sorted(
        (previously_managed_schemas - desired_schema_names) & live_schemas
    )
    orphan_extensions = sorted(
        (previously_managed_extensions - desired_extension_names)
        & (live_extensions - BASELINE_EXTENSIONS)
    )

    if retention == DROP:
        for schema in orphan_schemas:
            # Say what was destroyed. CASCADE on a populated schema is the most
            # consequential thing this operator does, so it is not silent.
            empty = await db.schema_is_empty(conn, schema)
            await db.drop_schema(conn, schema, cascade=True)
            detail = "" if empty else " (CASCADE, schema was not empty)"
            log.warning("dropped orphaned schema %s%s", schema, detail)
            result.changes.append(f"dropped orphaned schema {schema}{detail}")
        for extension in orphan_extensions:
            await db.drop_extension(conn, extension, cascade=True)
            result.changes.append(f"dropped orphaned extension {extension}")
        return

    result.orphaned_schemas = orphan_schemas
    result.orphaned_extensions = orphan_extensions


# ---------------------------------------------------------------------------
# teardown
# ---------------------------------------------------------------------------


async def teardown_database(ctx: ReconcileContext, obj: dict[str, Any]) -> str:
    """Apply the retention policy to a deleted PostgresDB.

    RETAIN leaves the database and its group roles in place. DROP terminates
    sessions, drops the database, and then drops the three group roles.
    """
    meta = obj.get("metadata") or {}
    namespace = str(meta.get("namespace"))
    name = str(meta.get("name"))
    spec = _parse_spec(obj)

    # Decide the policy from the specs alone. A RETAIN teardown touches nothing
    # on the server, so it must complete even while the server is unreachable
    # or its credentials Secret is missing.
    instance_spec, _ = await ctx.load_instance_spec(
        spec.instance_ref.name, for_namespace=namespace
    )
    database, roles = _derive_names(spec, name, instance_spec.role_prefix)
    retention = effective_retention(spec.retention_policy, instance_spec.retention_policy)

    if retention != DROP:
        log.info(
            "retention policy RETAIN: leaving database %s and roles %s in place",
            database,
            ", ".join(roles.all),
        )
        return (
            f"retained database {database} and group roles "
            f"{', '.join(roles.all)} on {instance_spec.host}:{instance_spec.port}"
        )

    instance = await ctx.resolve_instance(spec.instance_ref.name, for_namespace=namespace)

    # The operator's own pool must be released, or it counts as an open
    # connection and blocks the drop.
    await ctx.pools.close_database(instance.name, database)

    async with ctx.connect(instance) as conn:
        state = await db.get_database(conn, database)
        if state is not None:
            await _drop_database_safely(conn, database, instance)

        dropped: list[str] = []
        blocked: list[str] = []
        for role in roles.all:
            if not await pgroles.role_exists(conn, role):
                continue
            if await pgroles.owned_objects_exist(conn, role):
                # Still owns another database — most often a second PostgresDB
                # pointed at the same role names.
                blocked.append(role)
                continue
            await pgroles.drop_role(conn, role)
            dropped.append(role)

    summary = f"dropped database {database}"
    if dropped:
        summary += f" and role(s) {', '.join(dropped)}"
    if blocked:
        summary += (
            f"; retained role(s) {', '.join(blocked)} because they still own "
            "other databases"
        )
    return summary


async def _drop_database_safely(
    conn: AsyncConnection, database: str, instance: ResolvedInstance
) -> None:
    """Close the database to new sessions, evict the rest, then drop it.

    ``ALLOW_CONNECTIONS false`` first, on every version, so a client cannot
    reconnect between the terminate and the drop. ``WITH (FORCE)`` is preferred
    where available but needs the right to signal other users' backends, which
    a non-superuser may lack — so a plain drop is tried as a fallback. If the
    drop cannot be completed at all, connections are re-enabled before the
    error propagates, rather than leaving a database nobody can reach.
    """
    await db.set_allow_connections(conn, database, False)
    try:
        # Membership in a backend's role is what permits signalling it. The
        # operator has ADMIN OPTION on the login roles it created, so it can
        # grant itself what it needs for the application's own sessions.
        for role in await db.connection_roles(conn, database):
            try:
                await pgroles.ensure_self_membership(
                    conn,
                    role,
                    per_grant_options=instance.server.supports_per_grant_options,
                )
            except ReconcileFailure:
                # A role the operator did not create; the terminate below
                # reports it rather than failing the teardown.
                log.debug("cannot take membership of %s to signal its sessions", role)

        terminated, unsignalled = await db.terminate_connections(conn, database)
        if terminated:
            log.info("terminated %d connection(s) to %s", terminated, database)
        if unsignalled:
            log.warning(
                "sessions on %s owned by %s could not be terminated; the drop "
                "may fail until they disconnect",
                database,
                ", ".join(unsignalled),
            )
        try:
            await db.drop_database(
                conn, database, force=instance.server.supports_drop_database_force
            )
        except ReconcileFailure:
            if not instance.server.supports_drop_database_force:
                raise
            log.info(
                "DROP DATABASE %s WITH (FORCE) was refused; retrying without it",
                database,
            )
            await db.drop_database(conn, database, force=False)
    except Exception:
        await db.set_allow_connections(conn, database, True)
        raise


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def build_status(
    result: DatabaseResult,
    instance: ResolvedInstance,
    existing: dict[str, Any] | None,
    generation: int,
) -> dict[str, Any]:
    """Turn a reconcile result into the resource's status."""
    conditions = [
        st.reachable(f"PostgreSQL {instance.server.version} at {instance.display}", generation),
    ]

    orphans: list[str] = []
    if result.orphaned_schemas:
        orphans.append(
            "schema(s) " + ", ".join(result.orphaned_schemas) + " no longer declared"
        )
    if result.orphaned_extensions:
        orphans.append(
            "extension(s) "
            + ", ".join(result.orphaned_extensions)
            + " no longer declared"
        )

    unowned: list[str] = []
    if result.unowned_schemas:
        unowned.append(
            "cannot take ownership of schema(s) "
            + ", ".join(result.unowned_schemas)
            + f"; grants for {result.roles.read_write} and {result.roles.read_only} "
            "on them will not apply. Before PostgreSQL 15 the public schema "
            "belongs to the bootstrap superuser and a non-superuser managing "
            "role cannot reassign it: either run "
            f"ALTER SCHEMA public OWNER TO {result.roles.owner} as a superuser, "
            "declare application schemas instead of using public, or upgrade to "
            "PostgreSQL 15 or later where public belongs to pg_database_owner"
        )

    # Every way live state can diverge from the spec, kept categorised so both
    # conditions below can name the actual cause. Any number of these can be
    # true at once, and hiding one behind another would send the reader chasing
    # a problem they cannot see.
    #
    # Orphans are divergence the operator *will not* fix, because retention
    # policy forbids it. The rest is divergence it *cannot* fix: no privilege to
    # take a schema, to set a parameter, or no ALTER at all for the field. Only
    # the latter kind means the spec is unsatisfiable, so only it clears Synced.
    divergence: list[tuple[str, list[str]]] = []
    if orphans:
        divergence.append(("RetainedOrphans", orphans))
    if result.unowned_schemas:
        divergence.append(("SchemaNotOwned", unowned))
    if result.immutable_drift:
        divergence.append(("ImmutableFieldDrift", result.immutable_drift))
    if result.denied_parameters:
        divergence.append(("ParameterDenied", result.denied_parameters))
    if result.unmanageable_creators:
        divergence.append(
            (
                "DefaultPrivilegesDenied",
                [
                    "setRoleForOwners is off but the managing role cannot act as "
                    "owner-group member(s) "
                    + ", ".join(result.unmanageable_creators)
                    + f", so default privileges for {result.roles.read_write} and "
                    f"{result.roles.read_only} on objects they create cannot be set; "
                    "grant the managing role membership of those roles, or manage "
                    "them as PostgresUsers"
                ],
            )
        )

    def _collapse(entries: list[tuple[str, list[str]]]) -> tuple[str, str]:
        reason = entries[0][0] if len(entries) == 1 else "MultipleIssues"
        return reason, "; ".join(msg for _, messages in entries for msg in messages)

    if divergence:
        drift_reason, drift_message = _collapse(divergence)
        conditions.append(
            st.drifted(
                drift_message
                + (
                    "; retentionPolicy is RETAIN so nothing was dropped"
                    if orphans and result.retention != DROP
                    else ""
                ),
                generation,
                reason=drift_reason,
            )
        )
    else:
        conditions.append(st.no_drift(generation))

    blocking = [entry for entry in divergence if entry[0] != "RetainedOrphans"]
    if blocking:
        conditions.append(st.not_synced(*_collapse(blocking), generation))
    else:
        conditions.append(st.synced(result.message, generation))

    conditions.append(st.ready(result.message, generation))
    merged = st.merge_conditions((existing or {}).get("conditions"), conditions)

    return {
        "observedGeneration": generation,
        "conditions": merged,
        "phase": st.phase_from_conditions(merged),
        "instance": instance.name,
        "endpoint": instance.display,
        "databaseName": result.database,
        "retentionPolicy": result.retention,
        "roles": {
            "owner": result.roles.owner,
            "readWrite": result.roles.read_write,
            "readOnly": result.roles.read_only,
        },
        "schemaCount": len(result.managed_schemas),
        "extensionCount": len(result.managed_extensions),
        "managedSchemas": result.managed_schemas,
        "managedExtensions": result.managed_extensions,
        "observedSchemas": sorted(result.observed.schema_names),
        "observedExtensions": [
            f"{ext.name}@{ext.version}"
            for ext in sorted(result.observed.extensions, key=lambda e: e.name)
        ],
        "orphanedSchemas": result.orphaned_schemas,
        "orphanedExtensions": result.orphaned_extensions,
        "unmanagedSchemas": result.unmanaged_schemas,
        "unownedSchemas": result.unowned_schemas,
        "deniedParameters": result.denied_parameters,
        "unmanageableCreators": result.unmanageable_creators,
        "schemaGrants": result.schema_grants,
        "lastReconciledAt": st.now(),
    }


PLURAL = PLURAL_DATABASE
