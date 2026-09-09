"""PostgresUser reconciliation.

A PostgresUser is a LOGIN role plus a set of group memberships. It never holds
object privileges directly: each ``spec.access`` entry resolves the referenced
PostgresDB to one of its three group roles and grants membership of it. Changing
``RW`` to ``RO`` is therefore a revoke-and-grant of two memberships, with no
object-level work at all.

Memberships the operator granted are recorded in ``status.grants``. Revocation
only ever considers that record, so a membership added by a DBA out-of-band is
left alone instead of being stripped on the next pass.

Credentials: the password is generated once and then reused from the Secret the
operator wrote, so a steady-state reconcile performs no credential write. If the
Secret has been deleted the password is unrecoverable, so a fresh one is
generated and applied with ``ALTER ROLE`` — self-healing, not scheduled
rotation, which this iteration does not implement.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from psycopg import AsyncConnection

from .. import status as st
from ..constants import (
    ACCESS_OWNER,
    DROP,
    KIND_USER,
    PLURAL_USER,
)
from ..errors import ConfigurationError, ReferenceNotFound
from ..k8s.pushsecret import (
    apply_push_secret,
    build_push_secret,
    delete_push_secret,
    push_secret_status,
    render_remote_key,
)
from ..k8s.resources import get_database as get_database_cr
from ..k8s.secrets import (
    apply_secret,
    connection_keys,
    delete_secret,
    generate_password,
    managed_labels,
    read_secret,
)
from ..models import (
    PushSecretSpec,
    RetentionPolicy,
    UserSpec,
    effective_retention,
    parse_database_spec,
    parse_user_spec,
)
from ..naming import secret_name as default_secret_name
from ..postgres import database as db
from ..postgres import roles as pgroles
from ..postgres.privileges import GroupRoles
from .context import ReconcileContext, ResolvedInstance

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Grant:
    """One resolved access entry: a group role in a concrete database."""

    database: str
    level: str
    group_role: str
    db_resource: str
    #: From the referenced PostgresDB's spec.setRoleForOwners.
    set_role_for_owners: bool = True

    def as_status(self) -> dict[str, str]:
        return {
            "database": self.database,
            "role": self.level,
            "groupRole": self.group_role,
            "dbRef": self.db_resource,
        }

    @staticmethod
    def from_status(entry: dict[str, Any]) -> Grant:
        return Grant(
            database=str(entry.get("database", "")),
            level=str(entry.get("role", "")),
            group_role=str(entry.get("groupRole", "")),
            db_resource=str(entry.get("dbRef", "")),
        )


@dataclass(slots=True)
class UserResult:
    """Outcome of one user reconcile."""

    username: str
    grants: list[Grant] = field(default_factory=list)
    created: bool = False
    password_written: bool = False
    changes: list[str] = field(default_factory=list)
    secret_namespace: str = ""
    secret_name: str = ""
    secret_keys: list[str] = field(default_factory=list)
    push_secret_name: str | None = None
    push_secret_namespace: str | None = None
    warnings: list[str] = field(default_factory=list)
    retention: RetentionPolicy = "RETAIN"

    @property
    def message(self) -> str:
        if self.created:
            return f"created role {self.username} with {len(self.grants)} grant(s)"
        if self.changes:
            return f"converged {self.username}: " + "; ".join(self.changes[:8])
        return f"{self.username} in sync with {len(self.grants)} grant(s)"


async def reconcile_user(
    ctx: ReconcileContext, obj: dict[str, Any]
) -> tuple[UserResult, ResolvedInstance]:
    """Converge one PostgresUser: role, memberships, Secret and PushSecret."""
    meta = obj.get("metadata") or {}
    namespace = str(meta.get("namespace"))
    name = str(meta.get("name"))
    existing_status = obj.get("status") or {}

    spec = _parse_spec(obj)
    username = _derive_username(spec, name)

    instance = await ctx.resolve_instance(spec.instance_ref.name, for_namespace=namespace)
    retention = effective_retention(spec.retention_policy, instance.spec.retention_policy)

    result = UserResult(username=username, retention=retention)
    result.grants = await _resolve_grants(ctx, spec, instance, namespace)

    secret_namespace = spec.resolve_secret_namespace(namespace)
    secret_name = spec.resolve_secret_name(name)
    result.secret_namespace = secret_namespace
    result.secret_name = secret_name

    password, password_source = await _resolve_password(
        ctx, spec, namespace, secret_namespace, secret_name
    )

    # -- postgres -----------------------------------------------------------
    async with ctx.connect(instance) as conn:
        created = await pgroles.ensure_login_role(
            conn,
            username,
            password=password,
            attributes={**spec.attributes.as_mapping(), "login": spec.login},
            connection_limit=spec.connection_limit,
            valid_until=spec.valid_until,
        )
        result.created = created
        if created:
            result.changes.append(f"created role {username}")
            result.password_written = True
        else:
            await _ensure_password(conn, username, password, password_source, result)

        await _apply_memberships(conn, username, result, existing_status)
        await _apply_default_roles(conn, username, result, existing_status)

        observed_params = await pgroles.get_role_parameters(conn, username)
        param_changes = await pgroles.set_role_parameters(
            conn, username, spec.parameters, observed_params
        )
        result.changes.extend(param_changes)

    # -- kubernetes ---------------------------------------------------------
    await _write_credentials(ctx, spec, instance, namespace, name, result, password)
    await _sync_push_secret(ctx, spec, instance, namespace, name, result)

    return result, instance


def _parse_spec(obj: dict[str, Any]) -> UserSpec:
    try:
        return parse_user_spec(obj.get("spec") or {})
    except Exception as exc:
        raise ConfigurationError(f"invalid spec: {exc}") from exc


def _derive_username(spec: UserSpec, resource_name: str) -> str:
    """The login role name, or a configuration error if none can be derived.

    A resource name that sanitises to nothing, or to a reserved ``pg_`` name,
    can never be created; reporting it as misconfiguration beats retrying it.
    """
    try:
        return spec.resolve_username(resource_name)
    except ValueError as exc:
        raise ConfigurationError(f"invalid spec: {exc}") from exc


# ---------------------------------------------------------------------------
# grants
# ---------------------------------------------------------------------------


async def _resolve_grants(
    ctx: ReconcileContext,
    spec: UserSpec,
    instance: ResolvedInstance,
    namespace: str,
) -> list[Grant]:
    """Turn each ``spec.access`` entry into a concrete group role.

    The referenced PostgresDB must live on the same instance — a grant cannot
    span PostgreSQL clusters, and silently ignoring the mismatch would leave a
    user believing they had access they do not have.
    """
    grants: list[Grant] = []
    seen: set[tuple[str, str]] = set()
    for access in spec.access:
        ref_namespace = access.db_ref.resolve_namespace(namespace)
        # The model validator catches literal duplicates, but a reference with
        # the namespace spelled out and one relying on the default are the same
        # database and only become comparable once resolved.
        if (ref_namespace, access.db_ref.name) in seen:
            raise ConfigurationError(
                f"spec.access lists PostgresDB {ref_namespace}/{access.db_ref.name} "
                "more than once; a user holds exactly one access level per database"
            )
        seen.add((ref_namespace, access.db_ref.name))

        db_obj = await get_database_cr(ctx.k8s, ref_namespace, access.db_ref.name)
        try:
            db_spec = parse_database_spec(db_obj.get("spec") or {})
            database = db_spec.resolve_database_name(str(db_obj["metadata"]["name"]))
            group_roles = GroupRoles.from_mapping(
                db_spec.resolve_role_names(database, instance.spec.role_prefix)
            )
        except ValueError as exc:
            raise ConfigurationError(
                f"PostgresDB {ref_namespace}/{access.db_ref.name} has an invalid "
                f"spec: {exc}"
            ) from exc

        if db_spec.instance_ref.name != spec.instance_ref.name:
            raise ConfigurationError(
                f"PostgresDB {ref_namespace}/{access.db_ref.name} belongs to instance "
                f"{db_spec.instance_ref.name!r} but this user targets "
                f"{spec.instance_ref.name!r}; a grant cannot span instances"
            )
        grants.append(
            Grant(
                database=database,
                level=access.role,
                group_role=group_roles.for_level(access.role_key),
                db_resource=f"{ref_namespace}/{access.db_ref.name}",
                set_role_for_owners=db_spec.set_role_for_owners,
            )
        )
    return grants


async def _apply_memberships(
    conn: AsyncConnection,
    username: str,
    result: UserResult,
    existing_status: dict[str, Any],
) -> None:
    """Converge group memberships against the previously recorded set."""
    desired = {grant.group_role for grant in result.grants}
    previously_granted = {
        Grant.from_status(entry).group_role
        for entry in (existing_status.get("grants") or [])
    } - {""}
    held = await pgroles.memberships(conn, username)

    for group_role in sorted(desired - held):
        if not await pgroles.role_exists(conn, group_role):
            # The PostgresDB has not reconciled yet; retrying picks it up.
            result.warnings.append(
                f"group role {group_role} does not exist yet; waiting for its "
                "PostgresDB to reconcile"
            )
            continue
        await pgroles.grant_membership(conn, group_role, username)
        result.changes.append(f"granted {group_role}")

    # Only memberships this operator granted are eligible for revocation.
    for group_role in sorted((previously_granted - desired) & held):
        await pgroles.revoke_membership(conn, group_role, username)
        result.changes.append(f"revoked {group_role}")


async def _apply_default_roles(
    conn: AsyncConnection,
    username: str,
    result: UserResult,
    existing_status: dict[str, Any],
) -> None:
    """Set (or clear) the per-database default ``role`` for OWNER grants.

    Without this a human or migration job in the owner group creates tables
    owned by their own login role, so the owner group's DEFAULT PRIVILEGES never
    fire and the RW role cannot see the new tables.
    """
    # Whether to do this is the referenced PostgresDB's call, since it owns
    # the group roles and their default privileges.
    owner_grants = {
        grant.database: grant.group_role
        for grant in result.grants
        if grant.level == ACCESS_OWNER and grant.set_role_for_owners
    }
    previous_owner_dbs = {
        Grant.from_status(entry).database
        for entry in (existing_status.get("grants") or [])
        if str(entry.get("role")) == ACCESS_OWNER
    } - {""}

    resources = {grant.database: grant.db_resource for grant in result.grants}
    for database, group_role in sorted(owner_grants.items()):
        # ALTER ROLE ... IN DATABASE fails outright if the database is absent,
        # which happens at both ends of a database's life: before its PostgresDB
        # has reconciled, and while it is being deleted with users still
        # referencing it. Neither is this user's fault, and neither is fixed by
        # failing - the group membership is already granted and the next pass
        # picks up the rest.
        if await db.get_database(conn, database) is None:
            result.warnings.append(
                f"database {database} does not exist; waiting for PostgresDB "
                f"{resources.get(database, database)} to converge"
            )
            continue
        observed = await pgroles.get_role_parameters(conn, username, database)
        if observed.get("role") != group_role:
            await pgroles.set_default_role_in_database(
                conn, username, database, group_role
            )
            result.changes.append(f"set default role in {database} to {group_role}")

    for database in sorted(previous_owner_dbs - owner_grants.keys()):
        observed = await pgroles.get_role_parameters(conn, username, database)
        if "role" in observed:
            await pgroles.reset_default_role_in_database(conn, username, database)
            result.changes.append(f"cleared default role in {database}")


# ---------------------------------------------------------------------------
# credentials
# ---------------------------------------------------------------------------


async def _resolve_password(
    ctx: ReconcileContext,
    spec: UserSpec,
    namespace: str,
    secret_namespace: str,
    secret_name: str,
) -> tuple[str, str]:
    """Return ``(password, source)`` where source is ``byo``, ``existing`` or ``generated``."""
    if spec.password_secret_ref is not None:
        ref = spec.password_secret_ref
        data = await read_secret(
            ctx.k8s, ref.resolve_namespace(namespace), ref.name
        )
        if ref.key not in data:
            available = ", ".join(sorted(data)) or "<none>"
            raise ConfigurationError(
                f"passwordSecretRef {ref.resolve_namespace(namespace)}/{ref.name} "
                f"has no key {ref.key!r} (keys present: {available})"
            )
        return data[ref.key].decode(), "byo"

    key = spec.secret_key("password")
    try:
        existing = await read_secret(ctx.k8s, secret_namespace, secret_name)
    except ReferenceNotFound:
        # Normal on the first pass, and on a pass after the Secret was deleted.
        existing = {}
    if existing.get(key):
        return existing[key].decode(), "existing"

    return generate_password(spec.password_length), "generated"


async def _ensure_password(
    conn: AsyncConnection,
    username: str,
    password: str,
    source: str,
    result: UserResult,
) -> None:
    """Make the server's password agree with the credential we will publish.

    ``password_matches`` returns ``None`` when the check is impossible — the
    usual case on RDS, where ``pg_authid`` is not readable. A generated password
    is then applied anyway (the Secret was missing, so nothing else knows the
    old one), while a BYO or already-published password is left alone rather
    than rewritten on a guess.
    """
    matches = await pgroles.password_matches(conn, username, password)
    if matches is True:
        return

    if matches is False:
        await pgroles.set_password(conn, username, password)
        result.password_written = True
        result.changes.append("reset password to match the published credential")
        return

    if source == "generated":
        await pgroles.set_password(conn, username, password)
        result.password_written = True
        result.changes.append(
            "credentials Secret was missing; generated and applied a new password"
        )
    elif source == "byo":
        await pgroles.set_password(conn, username, password)
        result.password_written = True
        result.changes.append("applied password from passwordSecretRef")


async def _write_credentials(
    ctx: ReconcileContext,
    spec: UserSpec,
    instance: ResolvedInstance,
    namespace: str,
    name: str,
    result: UserResult,
    password: str,
) -> None:
    """Write the credentials Secret in the consuming namespace."""
    default_database = spec.generated_secret.default_database or (
        result.grants[0].database if result.grants else instance.spec.maintenance_database
    )
    logical = connection_keys(
        username=result.username,
        password=password,
        host=instance.endpoint.host,
        port=instance.endpoint.port,
        database=default_database,
        ssl_mode=instance.spec.ssl_mode,
        include_uri=spec.generated_secret.include_uri,
    )
    data = {spec.secret_key(key): value for key, value in logical.items()}
    result.secret_keys = sorted(data)

    # The owner labels name the PostgresUser, not the role or the Secret's own
    # namespace: they are what leads from a Secret in another namespace back to
    # the resource that produced it, and what a label selector matches on.
    labels = managed_labels(
        owner_kind=KIND_USER,
        owner_name=name,
        owner_namespace=namespace,
        instance=instance.name,
        extra=spec.generated_secret.labels,
    )
    created, changed = await apply_secret(
        ctx.k8s,
        namespace=result.secret_namespace,
        name=result.secret_name,
        data=data,
        labels=labels,
        annotations=spec.generated_secret.annotations,
    )
    if created:
        result.changes.append(f"created Secret {result.secret_namespace}/{result.secret_name}")
    elif changed:
        result.changes.append(f"updated Secret {result.secret_namespace}/{result.secret_name}")


async def _sync_push_secret(
    ctx: ReconcileContext,
    spec: UserSpec,
    instance: ResolvedInstance,
    namespace: str,
    name: str,
    result: UserResult,
) -> None:
    """Create, update or remove the PushSecret mirroring this user's Secret."""
    push_spec = _effective_push_secret(spec, instance)
    push_name = default_secret_name(name, "pg-push")
    result.push_secret_namespace = result.secret_namespace

    if push_spec is None or not push_spec.enabled or not ctx.settings.pushsecret_enabled:
        removed = await delete_push_secret(
            ctx.k8s,
            ctx.settings.pushsecret_api_version,
            result.secret_namespace,
            push_name,
        )
        if removed:
            result.changes.append(f"deleted PushSecret {result.secret_namespace}/{push_name}")
        result.push_secret_name = None
        result.push_secret_namespace = None
        return

    remote_key = render_remote_key(
        push_spec.remote_ref_key or f"{namespace}/{name}",
        namespace=namespace,
        name=name,
        username=result.username,
    )
    body = build_push_secret(
        push_spec,
        api_version=ctx.settings.pushsecret_api_version,
        name=push_name,
        namespace=result.secret_namespace,
        source_secret=result.secret_name,
        secret_keys=result.secret_keys,
        remote_key=remote_key,
        labels=managed_labels(
            owner_kind=KIND_USER,
            owner_name=name,
            owner_namespace=namespace,
            instance=instance.name,
        ),
    )
    created, changed = await apply_push_secret(ctx.k8s, ctx.settings, body)
    result.push_secret_name = push_name
    if created:
        result.changes.append(f"created PushSecret {result.secret_namespace}/{push_name}")
    elif changed:
        result.changes.append(f"updated PushSecret {result.secret_namespace}/{push_name}")
    else:
        # Only worth checking in steady state: a freshly created PushSecret has
        # no status yet, and external-secrets owns the actual push.
        await _check_push_secret_health(ctx, result, push_name)


async def _check_push_secret_health(
    ctx: ReconcileContext, result: UserResult, push_name: str
) -> None:
    """Report a PushSecret that external-secrets could not sync.

    Without this the credential looks published while the external store never
    received it — the failure would only be visible on the PushSecret itself.
    """
    status = await push_secret_status(
        ctx.k8s,
        ctx.settings.pushsecret_api_version,
        result.secret_namespace or "",
        push_name,
    )
    if not status:
        return
    for condition in status.get("conditions") or []:
        if condition.get("type") == "Ready" and condition.get("status") == "False":
            result.warnings.append(
                f"PushSecret {result.secret_namespace}/{push_name} is not synced: "
                f"{condition.get('reason', 'Unknown')}: {condition.get('message', '')}"
            )


def _effective_push_secret(
    spec: UserSpec, instance: ResolvedInstance
) -> PushSecretSpec | None:
    """A user's own pushSecret wins; otherwise the instance-wide default applies."""
    if spec.push_secret is not None:
        return spec.push_secret
    return instance.spec.push_secret


# ---------------------------------------------------------------------------
# teardown
# ---------------------------------------------------------------------------


async def teardown_user(ctx: ReconcileContext, obj: dict[str, Any]) -> str:
    """Apply the retention policy to a deleted PostgresUser.

    RETAIN leaves the role and its credentials Secret in place, so anything
    already using them keeps working. DROP terminates the role's sessions,
    clears its privileges in every granted database, drops the role, and removes
    the Secret and PushSecret.
    """
    meta = obj.get("metadata") or {}
    namespace = str(meta.get("namespace"))
    name = str(meta.get("name"))
    existing_status = obj.get("status") or {}
    spec = _parse_spec(obj)
    username = _derive_username(spec, name)
    secret_namespace = spec.resolve_secret_namespace(namespace)
    secret_name = spec.resolve_secret_name(name)

    # Decide the policy from the specs alone. A RETAIN teardown touches nothing
    # on the server, so it must complete even while the server is unreachable
    # or its credentials Secret is missing.
    instance_spec, _ = await ctx.load_instance_spec(
        spec.instance_ref.name, for_namespace=namespace
    )
    retention = effective_retention(spec.retention_policy, instance_spec.retention_policy)

    if retention != DROP:
        log.info("retention policy RETAIN: leaving role %s in place", username)
        return (
            f"retained role {username} and Secret {secret_namespace}/{secret_name} "
            f"on {instance_spec.host}:{instance_spec.port}"
        )

    instance = await ctx.resolve_instance(spec.instance_ref.name, for_namespace=namespace)

    databases = sorted(
        {
            Grant.from_status(entry).database
            for entry in (existing_status.get("grants") or [])
        }
        - {""}
    )

    async with ctx.connect(instance) as conn:
        if not await pgroles.role_exists(conn, username):
            role_summary = f"role {username} was already absent"
        else:
            # First, take the privileges of the role being removed. This is
            # needed twice over: DROP OWNED BY requires them, and signalling a
            # backend requires membership in the role running it. Creating the
            # role conferred only ADMIN OPTION.
            await pgroles.ensure_self_membership(
                conn,
                username,
                per_grant_options=instance.server.supports_per_grant_options,
            )
            terminated = await pgroles.terminate_role_sessions(conn, username)
            if terminated:
                log.info("terminated %d session(s) for %s", terminated, username)
            revoked = await pgroles.revoke_all_memberships(conn, username)
            for database in databases:
                # A PostgresDB and the PostgresUsers referencing it are usually
                # deleted together, so by now the database may be gone. Dropping
                # it already removed this role's grants, objects and per-database
                # settings there, and connecting to it would only stall for the
                # connect timeout before failing.
                if await db.get_database(conn, database) is None:
                    log.info(
                        "database %s is already gone; nothing to clear for %s",
                        database,
                        username,
                    )
                    continue
                await _clear_user_in_database(ctx, instance, database, username)
                await pgroles.reset_default_role_in_database(conn, username, database)
            await pgroles.drop_role(conn, username)
            role_summary = f"dropped role {username}"
            if revoked:
                role_summary += f" (revoked {', '.join(revoked)})"

    removed_secret = await delete_secret(ctx.k8s, secret_namespace, secret_name)
    removed_push = await delete_push_secret(
        ctx.k8s,
        ctx.settings.pushsecret_api_version,
        secret_namespace,
        default_secret_name(name, "pg-push"),
    )

    parts = [role_summary]
    if removed_secret:
        parts.append(f"deleted Secret {secret_namespace}/{secret_name}")
    if removed_push:
        parts.append("deleted PushSecret")
    return "; ".join(parts)


async def _clear_user_in_database(
    ctx: ReconcileContext, instance: ResolvedInstance, database: str, username: str
) -> None:
    """``DROP OWNED BY`` inside one database, so DROP ROLE is not blocked.

    This removes the role's own grants and any objects it owns in that database.
    With ``setRoleForOwners`` enabled, objects created by owner-group members
    belong to the group role rather than the login role, so this does not
    destroy application tables.
    """
    try:
        async with ctx.connect(instance, database) as conn:
            await pgroles.drop_owned(conn, username)
    except Exception as exc:  # noqa: BLE001 - a dropped database is not an error here
        log.info(
            "could not clear %s in database %s (it may already be gone): %s",
            username,
            database,
            exc,
        )
        # The pool is registered before its first connection succeeds, so a
        # database that vanished would otherwise leave one behind retrying in
        # the background until max_idle expires.
        await ctx.pools.close_database(instance.name, database)


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def build_status(
    result: UserResult,
    instance: ResolvedInstance,
    existing: dict[str, Any] | None,
    generation: int,
) -> dict[str, Any]:
    conditions = [
        st.reachable(
            f"PostgreSQL {instance.server.version} at {instance.display}", generation
        )
    ]
    if result.warnings:
        conditions.append(
            st.not_synced("PendingDependency", "; ".join(result.warnings), generation)
        )
        conditions.append(
            st.not_ready("PendingDependency", "; ".join(result.warnings), generation)
        )
    else:
        conditions.append(st.synced(result.message, generation))
        conditions.append(st.ready(result.message, generation))
    conditions.append(st.no_drift(generation))

    merged = st.merge_conditions((existing or {}).get("conditions"), conditions)
    payload: dict[str, Any] = {
        "observedGeneration": generation,
        "conditions": merged,
        "phase": st.phase_from_conditions(merged),
        "instance": instance.name,
        "endpoint": instance.display,
        "username": result.username,
        "retentionPolicy": result.retention,
        "grantCount": len(result.grants),
        "grants": [grant.as_status() for grant in result.grants],
        "secretRef": {
            "name": result.secret_name,
            "namespace": result.secret_namespace,
        },
        "secretKeys": result.secret_keys,
        "lastReconciledAt": st.now(),
    }
    if result.push_secret_name:
        payload["pushSecretRef"] = {
            "name": result.push_secret_name,
            "namespace": result.push_secret_namespace,
        }
    return payload


PLURAL = PLURAL_USER
