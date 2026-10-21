"""PostgresReplication reconciliation.

The design (``docs/design/replication.md``, "Reconciliation") converges a
replication in eight steps. This module implements the first five - resolve
both ends, check the servers' prerequisites, the replication login, the table
set, and the publication - and the teardown of everything they create. The
subscription arrives in a later version, so status reads ``Pending``: the
source side is converged, but nothing replicates yet.
"""

from __future__ import annotations

import contextlib
import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from psycopg import AsyncConnection, sql

from .. import status as st
from ..constants import KIND_REPLICATION, PLURAL_REPLICATION
from ..errors import ConfigurationError, ReconcileFailure, ReferenceNotFound
from ..k8s.resources import get_database, list_replications, patch_status
from ..k8s.secrets import apply_secret, delete_secret, generate_password, managed_labels, read_secret
from ..models import (
    CredentialsSecretRef,
    DatabaseSpec,
    InstanceSpec,
    ObjectRef,
    ReplicationSpec,
    parse_database_spec,
    parse_replication_spec,
)
from ..naming import ReplicationNames, replication_names
from ..postgres import database as pgdb
from ..postgres import publication as pub
from ..postgres import roles as pgroles
from ..postgres.filters import fingerprint, render_row_filter
from ..postgres.replication import (
    MIN_REPLICATION_VERSION,
    LogicalReplicationInfo,
    describe_logical_replication,
)
from ..postgres.sql import execute, fetch_scalar, ident, literal
from .context import ReconcileContext, ResolvedInstance
from .eligibility import (
    INVALID_ROW_FILTER,
    PUBLICATION_REFUSED,
    Verdict,
    candidates,
    judge,
    target_relations,
)

log = logging.getLogger(__name__)

#: A physical database: host, port, database name.
DatabaseIdentity = tuple[str, int, str]

#: Stamped as a COMMENT on every object the operator creates on a server, so
#: an object with the same name that someone else made is never taken over.
MARKER_PREFIX = "pg-operator: PostgresReplication"


def ownership_marker(namespace: str, name: str) -> str:
    return f"{MARKER_PREFIX} {namespace}/{name}"


@dataclass(frozen=True, slots=True)
class ReplicationEnd:
    """One side of a replication: its PostgresDB, resolved to a server and database."""

    namespace: str
    name: str
    instance: str
    database: str
    host: str
    port: int

    @property
    def server(self) -> tuple[str, int]:
        """The physical server. Two PostgresInstances may point at the same one."""
        return (self.host.lower(), self.port)

    @property
    def identity(self) -> DatabaseIdentity:
        """The physical database, whichever PostgresDB names it."""
        return (*self.server, self.database)

    @property
    def display(self) -> str:
        return f"{self.instance}/{self.database}"

    def as_status(self) -> dict[str, Any]:
        return {
            "instance": self.instance,
            "database": self.database,
            "postgresDB": f"{self.namespace}/{self.name}",
            "display": self.display,
        }


@dataclass(slots=True)
class ReplicationResult:
    source: ReplicationEnd
    target: ReplicationEnd
    names: ReplicationNames
    source_server: str = ""
    target_server: str = ""
    login: str = ""
    #: Where the login's credentials live, and whether the operator wrote them.
    credentials: dict[str, Any] = field(default_factory=dict)
    #: Which objects the operator created; teardown removes only these.
    created: dict[str, bool] = field(default_factory=dict)
    #: Memberships the operator granted the login; teardown revokes only these.
    granted: list[str] = field(default_factory=list)
    published: list[str] = field(default_factory=list)
    excluded: list[dict[str, str]] = field(default_factory=list)
    #: Published tables with a target-side problem, kept published (design D6).
    kept: list[dict[str, str]] = field(default_factory=list)
    warnings: list[dict[str, str]] = field(default_factory=list)
    row_filters: list[dict[str, str]] = field(default_factory=list)
    changes: list[str] = field(default_factory=list)

    @property
    def message(self) -> str:
        summary = (
            f"{self.source.display} -> {self.target.display}: publication "
            f"{self.names.publication} carries {len(self.published)} table(s)"
        )
        if self.excluded:
            summary += f", {len(self.excluded)} held back"
        if self.changes:
            return f"{summary}; " + "; ".join(self.changes[:6])
        return f"{summary}; this operator version does not create the subscription yet"


@dataclass(frozen=True, slots=True)
class Resolved:
    """Step 1's outcome: the spec, and both ends it points at."""

    spec: ReplicationSpec
    source: ReplicationEnd
    target: ReplicationEnd
    source_db: DatabaseSpec
    source_instance: InstanceSpec


async def resolve_replication(ctx: ReconcileContext, obj: dict[str, Any]) -> Resolved:
    """Step 1: resolve both ends and refuse what can never work.

    Reads Kubernetes only - no server is contacted - so a replication that
    could never work is refused before anything is created for it.
    """
    meta = obj.get("metadata") or {}
    namespace = str(meta.get("namespace"))
    name = str(meta.get("name"))
    spec = _parse_spec(obj)
    source, source_db, source_instance = await _resolve_end(
        ctx, spec.source.db_ref, namespace, side="source", for_namespace=namespace
    )
    target, _, _ = await _resolve_end(
        ctx, spec.target.db_ref, namespace, side="target", for_namespace=namespace
    )
    if source.server == target.server:
        raise ConfigurationError(
            f"source {source.display} and target {target.display} are on the same "
            f"server ({source.host}:{source.port}); replication within one server "
            "is not supported"
        )
    _require_managed_schemas(spec, source, source_db)
    await _refuse_cycles(ctx, namespace, name, source, target)
    return Resolved(spec, source, target, source_db, source_instance)


async def reconcile_replication(
    ctx: ReconcileContext, obj: dict[str, Any]
) -> ReplicationResult:
    """Converge the source side of a PostgresReplication."""
    meta = obj.get("metadata") or {}
    namespace = str(meta.get("namespace"))
    name = str(meta.get("name"))
    existing = obj.get("status") or {}

    # 1. Resolve both ends and refuse what can never work.
    resolved = await resolve_replication(ctx, obj)
    spec, source, target, source_db = (
        resolved.spec, resolved.source, resolved.target, resolved.source_db
    )
    result = ReplicationResult(
        source=source,
        target=target,
        names=_names(existing, namespace, name, resolved.source_instance.role_prefix),
        created={key: bool(value) for key, value in (existing.get("created") or {}).items()},
        granted=list(existing.get("grantedMemberships") or []),
    )

    # 2. Prerequisites: both servers new enough, the source logging logically.
    source_instance = await ctx.resolve_instance(source.instance, for_namespace=namespace)
    target_instance = await ctx.resolve_instance(target.instance, for_namespace=namespace)
    for side, instance in (("source", source_instance), ("target", target_instance)):
        if instance.server.major_version < MIN_REPLICATION_VERSION:
            raise ConfigurationError(
                f"{side} {instance.name} runs PostgreSQL {instance.server.version}; "
                f"replication requires {MIN_REPLICATION_VERSION} or later on both servers"
            )
    result.source_server = f"PostgreSQL {source_instance.server.version} at {source_instance.display}"
    result.target_server = f"PostgreSQL {target_instance.server.version} at {target_instance.display}"

    groups = source_db.resolve_role_names(source.database, source_instance.spec.role_prefix)
    marker = ownership_marker(namespace, name)
    async with ctx.connect(source_instance, source.database) as src:
        info = await describe_logical_replication(src, source_instance.server)
        if not info.wal_is_logical:
            (remedy,) = (issue for issue in info.issues() if issue.startswith("wal_level"))
            raise ConfigurationError(f"source {source.display}: {remedy}")

        # Nothing is created until every name is known to be free: a pass that
        # can never succeed must not leave objects behind it.
        await _refuse_foreign_objects(src, spec, result, marker=marker)

        # 3. The publication (empty for now) and the replication login. Each is
        # recorded in status the moment it exists, so a pass that fails later
        # cannot hide it from teardown.
        if await _ensure_publication(src, spec, result, marker=marker):
            await _record_created(ctx, obj, result)
        created_before, granted_before = dict(result.created), set(result.granted)
        await _ensure_login(
            ctx, src, spec, info, result,
            namespace=namespace, name=name, marker=marker, read_group=groups["readOnly"],
        )
        if result.created != created_before or set(result.granted) != granted_before:
            await _record_created(ctx, obj, result)

        # 4. The table set, judged against both catalogs.
        catalog = await pub.describe_source_tables(
            src,
            schemas=[selection.name for selection in spec.schemas],
            tables=[(entry.schema_, entry.name) for entry in spec.tables],
            login=result.login,
        )
        selected = candidates(spec, catalog)
        wanted = {
            (relation.schema, relation.name)
            for candidate in selected
            if (table := catalog.get(candidate.qualified)) is not None
            for relation in target_relations(table, via_root=spec.publish_via_partition_root)
        }
        async with ctx.connect(target_instance, target.database) as dst:
            targets = await pub.describe_target_tables(dst, wanted)
        claims = await _claims_elsewhere(ctx, obj, target)
        verdicts = [
            judge(
                candidate,
                catalog.get(candidate.qualified),
                targets,
                keyed=spec.publishes_keyed_changes,
                via_root=spec.publish_via_partition_root,
                owner_group=groups["owner"],
                read_group=groups["readOnly"],
                login=result.login,
                claimed_by=claims.get(candidate.qualified),
            )
            for candidate in selected
        ]

        # 5. The publication's tables.
        await _converge_tables(src, verdicts, existing, result)
    return result


def _parse_spec(obj: dict[str, Any]) -> ReplicationSpec:
    try:
        return parse_replication_spec(obj.get("spec") or {})
    except ValueError as exc:
        raise ConfigurationError(f"invalid spec: {exc}") from exc


def _names(
    existing: dict[str, Any], namespace: str, name: str, role_prefix: str
) -> ReplicationNames:
    """Names recorded in status win over derived ones, so a rename cannot orphan an object."""
    try:
        derived = replication_names(namespace, name, role_prefix)
    except ValueError as exc:
        raise ConfigurationError(f"cannot name this replication's objects: {exc}") from exc
    created = existing.get("created") or {}
    source = existing.get("source") or {}
    target = existing.get("target") or {}
    credentials = existing.get("credentialsSecret") or {}
    return ReplicationNames(
        publication=source.get("publication") or derived.publication,
        subscription=target.get("subscription") or derived.subscription,
        # The recorded login may be a bring-your-own one; only a login the
        # operator created is this replication's generated login.
        login=str((created.get("login") and source.get("login")) or derived.login),
        secret=str((created.get("secret") and credentials.get("name")) or derived.secret),
    )


async def _resolve_end(
    ctx: ReconcileContext,
    ref: ObjectRef,
    namespace: str,
    *,
    side: str,
    for_namespace: str | None,
) -> tuple[ReplicationEnd, DatabaseSpec, InstanceSpec]:
    """Follow a dbRef to its PostgresDB, then to that database's instance.

    ``for_namespace`` applies the instance's ``allowedNamespaces`` gate: this
    replication's namespace must be permitted on both ends, exactly as a
    PostgresUser's must be on its instance.
    """
    ref_namespace = ref.resolve_namespace(namespace)
    try:
        db_obj = await get_database(ctx.k8s, ref_namespace, ref.name)
    except ReferenceNotFound as exc:
        # Still temporary: the PostgresDB may simply not exist yet.
        raise type(exc)(f"spec.{side}: {exc}") from exc
    try:
        db_spec = parse_database_spec(db_obj.get("spec") or {})
        database = db_spec.resolve_database_name(str(db_obj["metadata"]["name"]))
    except ValueError as exc:
        raise ConfigurationError(
            f"{side} PostgresDB {ref_namespace}/{ref.name} has an invalid spec: {exc}"
        ) from exc

    instance_spec, _ = await ctx.load_instance_spec(
        db_spec.instance_ref.name, for_namespace=for_namespace
    )
    end = ReplicationEnd(
        namespace=ref_namespace,
        name=ref.name,
        instance=db_spec.instance_ref.name,
        database=database,
        host=instance_spec.host,
        port=instance_spec.port,
    )
    return end, db_spec, instance_spec


def _require_managed_schemas(
    spec: ReplicationSpec, source: ReplicationEnd, source_db: DatabaseSpec
) -> None:
    """Every selected schema must be one the source PostgresDB manages.

    The replication login reads through the source database's RO group, and
    that group holds grants on managed schemas only - anything else would fail
    its initial copy for want of SELECT.
    """
    managed = {schema.name for schema in source_db.resolve_schemas()}
    unmanaged = sorted(spec.selected_schemas - managed)
    if unmanaged:
        raise ConfigurationError(
            f"schema(s) {', '.join(unmanaged)} are not managed by source PostgresDB "
            f"{source.namespace}/{source.name}; its RO group, through which the "
            "replication login reads, holds grants on managed schemas only. Add them "
            "to that PostgresDB's spec.schemas"
        )


async def _refuse_cycles(
    ctx: ReconcileContext,
    namespace: str,
    name: str,
    source: ReplicationEnd,
    target: ReplicationEnd,
) -> None:
    """Refuse a replication that would close a loop of replications.

    A subscription republishes what it applies, so any cycle - not only A->B
    beside B->A, but A->B->C->A - sends each change round until a duplicate key
    stops apply. The graph is of physical databases, so two PostgresDBs naming
    the same database cannot hide a loop between them.
    """
    edges: dict[DatabaseIdentity, set[tuple[DatabaseIdentity, str]]] = {}
    for other in await list_replications(ctx.k8s):
        meta = other.get("metadata") or {}
        other_namespace = str(meta.get("namespace"))
        other_name = str(meta.get("name"))
        if (other_namespace, other_name) == (namespace, name) or meta.get("deletionTimestamp"):
            continue
        try:
            other_spec = parse_replication_spec(other.get("spec") or {})
            other_source, _, _ = await _resolve_end(
                ctx, other_spec.source.db_ref, other_namespace, side="source", for_namespace=None
            )
            other_target, _, _ = await _resolve_end(
                ctx, other_spec.target.db_ref, other_namespace, side="target", for_namespace=None
            )
        except (ConfigurationError, ValueError) as exc:
            # One that cannot resolve cannot be replicating either.
            log.debug("ignoring %s/%s in the cycle check: %s", other_namespace, other_name, exc)
            continue
        edges.setdefault(other_source.identity, set()).add(
            (other_target.identity, f"{other_namespace}/{other_name}")
        )

    loop = _path(edges, start=target.identity, goal=source.identity)
    if loop is not None:
        raise ConfigurationError(
            f"{source.display} -> {target.display} would close a replication cycle "
            f"with {', '.join(loop)}; changes would be applied round it until apply "
            "stalls on a duplicate key"
        )


def _path(
    edges: dict[DatabaseIdentity, set[tuple[DatabaseIdentity, str]]],
    *,
    start: DatabaseIdentity,
    goal: DatabaseIdentity,
) -> list[str] | None:
    """The replications on a path from ``start`` to ``goal``, if one exists."""
    queue: deque[tuple[DatabaseIdentity, list[str]]] = deque([(start, [])])
    seen = {start}
    while queue:
        node, via = queue.popleft()
        if node == goal:
            return via
        for following, replication in sorted(edges.get(node, ())):
            if following not in seen:
                seen.add(following)
                queue.append((following, [*via, replication]))
    return None


# ---------------------------------------------------------------------------
# the replication login
# ---------------------------------------------------------------------------


async def _ensure_login(
    ctx: ReconcileContext,
    conn: AsyncConnection,
    spec: ReplicationSpec,
    info: LogicalReplicationInfo,
    result: ReplicationResult,
    *,
    namespace: str,
    name: str,
    marker: str,
    read_group: str,
) -> None:
    """A login the target server can stream as, reading through the source's RO group."""
    if spec.source.credentials_secret_ref is not None:
        await _adopt_provided_login(
            ctx, conn, spec.source.credentials_secret_ref, result, namespace=namespace
        )
    else:
        await _ensure_generated_login(
            ctx, conn, info, result, namespace=namespace, name=name, marker=marker
        )
    # USAGE, not mere membership: the login must hold the group's SELECTs.
    await _grant(conn, read_group, result.login, result, privilege="USAGE")


async def _adopt_provided_login(
    ctx: ReconcileContext,
    conn: AsyncConnection,
    ref: CredentialsSecretRef,
    result: ReplicationResult,
    *,
    namespace: str,
) -> None:
    secret_namespace = ref.resolve_namespace(namespace)
    data = await read_secret(ctx.k8s, secret_namespace, ref.name)
    missing = [key for key in (ref.username_key, ref.password_key) if not data.get(key)]
    if missing:
        raise ConfigurationError(
            f"credentialsSecretRef {secret_namespace}/{ref.name} has no {', '.join(missing)}"
        )
    login = data[ref.username_key].decode().strip()
    role = await pgroles.get_role(conn, login)
    if role is None:
        raise ConfigurationError(
            f"credentialsSecretRef {secret_namespace}/{ref.name} names login {login!r}, "
            f"which does not exist on {result.source.instance}"
        )
    if not role.replication and not await _member_of(conn, login, "rds_replication"):
        raise ConfigurationError(
            f"login {login!r} from credentialsSecretRef holds neither REPLICATION nor "
            "rds_replication, so the target server could not stream as it"
        )
    result.login = login
    result.credentials = {"namespace": secret_namespace, "name": ref.name, "generated": False}


async def _ensure_generated_login(
    ctx: ReconcileContext,
    conn: AsyncConnection,
    info: LogicalReplicationInfo,
    result: ReplicationResult,
    *,
    namespace: str,
    name: str,
    marker: str,
) -> None:
    login = result.names.login
    if info.replication_grant == "none":
        raise ConfigurationError(
            f"the managing role on {result.source.instance} can neither grant rds_replication "
            "nor confer REPLICATION, so it cannot create a login the target could stream as; "
            "provide one with source.credentialsSecretRef"
        )
    secret_namespace = ctx.settings.operator_namespace
    secret_name = result.names.secret
    try:
        stored = (await read_secret(ctx.k8s, secret_namespace, secret_name)).get("password")
    except ReferenceNotFound:
        # Normal on the first pass, and after the Secret was deleted.
        stored = None
    password = stored.decode() if stored else generate_password()

    # REPLICATION is named only when it is how streaming is granted. Restating
    # it otherwise would emit NOREPLICATION, which a managing role without
    # REPLICATION - the RDS master user - is refused even as a no-op.
    attributes = {"login": True, "inherit": True}
    if info.replication_grant == "attribute":
        attributes["replication"] = True
    created = await pgroles.ensure_login_role(conn, login, password=password, attributes=attributes)
    if created:
        await execute(conn, sql.SQL("COMMENT ON ROLE {} IS {}").format(ident(login), literal(marker)))
        result.changes.append(f"created replication login {login}")
    elif stored is None:
        await pgroles.set_password(conn, login, password)
        result.changes.append("credentials Secret was missing; generated and applied a new password")
    result.created["login"] = True
    result.login = login

    if info.replication_grant == "rds_replication":
        await _grant(conn, "rds_replication", login, result, privilege="MEMBER")

    secret_created, secret_changed = await apply_secret(
        ctx.k8s,
        namespace=secret_namespace,
        name=secret_name,
        data={"username": login, "password": password},
        labels=managed_labels(
            owner_kind=KIND_REPLICATION,
            owner_name=name,
            owner_namespace=namespace,
            instance=result.source.instance,
        ),
    )
    if secret_created:
        result.changes.append(f"created Secret {secret_namespace}/{secret_name}")
    elif secret_changed:
        result.changes.append(f"updated Secret {secret_namespace}/{secret_name}")
    result.created["secret"] = True
    result.credentials = {"namespace": secret_namespace, "name": secret_name, "generated": True}


async def _grant(
    conn: AsyncConnection, group: str, member: str, result: ReplicationResult, *, privilege: str
) -> None:
    """Grant membership if missing, then read it back - GRANT only warns when refused."""
    has = sql.SQL("SELECT pg_has_role(%s, %s, %s)")
    if await fetch_scalar(conn, has, (member, group, privilege)):
        return
    await pgroles.grant_membership(conn, group, member)
    if not await fetch_scalar(conn, has, (member, group, privilege)):
        raise ReconcileFailure(
            f"GRANT {group} TO {member} was accepted but did not take effect; the managing "
            f"role needs ADMIN OPTION on {group}"
        )
    if group not in result.granted:
        result.granted.append(group)
    result.changes.append(f"granted {group} to {member}")


async def _member_of(conn: AsyncConnection, member: str, group: str) -> bool:
    exists = await fetch_scalar(
        conn, sql.SQL("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = %s)"), (group,)
    )
    return bool(exists) and bool(
        await fetch_scalar(conn, sql.SQL("SELECT pg_has_role(%s, %s, 'MEMBER')"), (member, group))
    )


async def _role_comment(conn: AsyncConnection, role: str) -> str | None:
    value = await fetch_scalar(
        conn,
        sql.SQL(
            "SELECT shobj_description(oid, 'pg_authid') FROM pg_roles WHERE rolname = %s"
        ),
        (role,),
    )
    return str(value) if value is not None else None


# ---------------------------------------------------------------------------
# other replications into the same target
# ---------------------------------------------------------------------------


async def _claims_elsewhere(
    ctx: ReconcileContext, obj: dict[str, Any], target: ReplicationEnd
) -> dict[str, str]:
    """Tables an older replication already publishes into the same target database.

    Two subscriptions writing one table conflict. The older replication keeps
    it - by creation time, then namespace and name - so the two cannot each
    defer to the other and both drop it.
    """
    meta = obj.get("metadata") or {}
    mine = (str(meta.get("creationTimestamp") or ""), str(meta.get("namespace")), str(meta.get("name")))
    claims: dict[str, str] = {}
    for other in await list_replications(ctx.k8s):
        other_meta = other.get("metadata") or {}
        rank = (
            str(other_meta.get("creationTimestamp") or ""),
            str(other_meta.get("namespace")),
            str(other_meta.get("name")),
        )
        if rank >= mine or other_meta.get("deletionTimestamp"):
            continue
        status = other.get("status") or {}
        other_target = status.get("target") or {}
        if other_target.get("database") != target.database or not other_target.get("instance"):
            continue
        try:
            instance_spec, _ = await ctx.load_instance_spec(str(other_target["instance"]))
        except ConfigurationError:
            continue
        if (instance_spec.host.lower(), instance_spec.port) != target.server:
            continue
        for table in status.get("publishedTables") or []:
            claims.setdefault(str(table), f"{rank[1]}/{rank[2]}")
    return claims


# ---------------------------------------------------------------------------
# the publication
# ---------------------------------------------------------------------------


async def _refuse_foreign_objects(
    conn: AsyncConnection, spec: ReplicationSpec, result: ReplicationResult, *, marker: str
) -> None:
    """Refuse to proceed if a name this replication needs belongs to someone else.

    Read-only, and run before anything is created. An object carrying this
    replication's marker - or recorded in status as created - is its own; any
    other object of that name is never taken over.
    """
    name = result.names.publication
    state = await pub.get_publication(conn, name)
    if state is not None and state.comment != marker and not result.created.get("publication"):
        raise ConfigurationError(
            f"a publication named {name} already exists in {result.source.display} and was "
            f"not created for this replication (its comment is {state.comment!r}); the "
            "operator will not take it over"
        )
    login = result.names.login
    if (
        spec.source.credentials_secret_ref is None
        and not result.created.get("login")
        and await pgroles.role_exists(conn, login)
    ):
        found = await _role_comment(conn, login)
        if found != marker:
            raise ConfigurationError(
                f"a role named {login} already exists on {result.source.instance} and was not "
                f"created for this replication (its comment is {found!r}); the operator will not "
                "take it over"
            )


async def _ensure_publication(
    conn: AsyncConnection, spec: ReplicationSpec, result: ReplicationResult, *, marker: str
) -> bool:
    """Create the publication, or converge its options. True if it was created."""
    name = result.names.publication
    state = await pub.get_publication(conn, name)
    result.created["publication"] = True
    if state is None:
        await pub.create_publication(
            conn, name, operations=spec.publish, via_root=spec.publish_via_partition_root, comment=marker
        )
        result.changes.append(f"created publication {name}")
        return True
    if state.operations != set(spec.publish) or state.via_root != spec.publish_via_partition_root:
        await pub.set_publication_options(
            conn, name, operations=spec.publish, via_root=spec.publish_via_partition_root
        )
        result.changes.append(f"set publish = {', '.join(spec.publish)}")
    return False


async def _record_created(ctx: ReconcileContext, obj: dict[str, Any], result: ReplicationResult) -> None:
    """Write what now exists to status, before the rest of the pass can fail.

    A failed pass publishes only its conditions. Without this, an object made
    early in it would be unknown to teardown - and would outlive a deletion
    that came before the next successful pass.
    """
    meta = obj.get("metadata") or {}
    await patch_status(
        ctx.k8s,
        PLURAL_REPLICATION,
        str(meta.get("name")),
        {
            "source": {
                **result.source.as_status(),
                "publication": result.names.publication,
                "slot": result.names.subscription,
                "login": result.login,
            },
            "credentialsSecret": result.credentials,
            "created": result.created,
            "grantedMemberships": sorted(set(result.granted)),
        },
        str(meta.get("namespace")),
    )


async def _converge_tables(
    conn: AsyncConnection,
    verdicts: list[Verdict],
    existing: dict[str, Any],
    result: ReplicationResult,
) -> None:
    """Make the publication carry exactly the publishable tables, as the spec shapes them."""
    name = result.names.publication
    state = await pub.get_publication(conn, name)
    current: dict[str, pub.PublishedTable] = dict(state.tables) if state is not None else {}

    recorded = {entry["table"]: entry for entry in existing.get("rowFilters") or []}
    by_table = {verdict.candidate.qualified: verdict for verdict in verdicts}

    for table in sorted(current):
        verdict = by_table.get(table)
        if verdict is None or not (verdict.publishable or verdict.keeps_membership):
            schema, relation = table.split(".", 1)
            await pub.drop_table(conn, name, schema, relation)
            result.changes.append(f"removed {table} from the publication")

    for verdict in verdicts:
        candidate = verdict.candidate
        table = candidate.qualified
        result.warnings.extend(warning.as_status(table) for warning in verdict.warnings)
        if not verdict.publishable:
            if table in current and verdict.keeps_membership:
                result.kept.extend(
                    {**issue.as_status(table), "message": f"{issue.message} (kept published: "
                     "removing it would not recall changes already sent)"}
                    for issue in verdict.issues
                )
            else:
                result.excluded.extend(issue.as_status(table) for issue in verdict.issues)
            continue

        row_filter = render_row_filter(candidate.row_filter) if candidate.row_filter else None
        if table not in current:
            action, verb = pub.add_table, "added"
        elif _member_matches(candidate, current[table], recorded.get(table)):
            continue
        else:
            action, verb = pub.replace_table, "updated"
        try:
            await action(
                conn, name, candidate.schema, candidate.name,
                columns=candidate.columns, row_filter=row_filter,
            )
        except ReconcileFailure as exc:
            reason = INVALID_ROW_FILTER if candidate.row_filter else PUBLICATION_REFUSED
            entry = {"table": table, "reason": reason, "message": f"the server refused it: {exc}"}
            if verb == "updated":
                # replace_table is atomic: the previous shape is still published.
                result.warnings.append({**entry, "message": entry["message"] + "; the previous shape is still published"})
            else:
                result.excluded.append(entry)
            continue
        result.changes.append(f"{verb} {table}")

    final = await pub.get_publication(conn, name)
    published = final.tables if final is not None else {}
    result.published = sorted(published)
    result.row_filters = [
        {
            "table": verdict.candidate.qualified,
            "specHash": fingerprint(verdict.candidate.row_filter),
            "applied": published[verdict.candidate.qualified].row_filter or "",
        }
        for verdict in verdicts
        if verdict.candidate.row_filter is not None and verdict.candidate.qualified in published
    ]


def _member_matches(
    candidate: Any, member: pub.PublishedTable, recorded: dict[str, str] | None
) -> bool:
    """Whether a published table already has the candidate's columns and filter.

    Columns compare directly. A filter cannot - the server re-deparses it - so
    it matches when the spec's hash and the server's text both equal what the
    last apply recorded. With nothing recorded it is re-applied once.
    """
    wanted_columns = frozenset(candidate.columns) or None
    if member.columns != wanted_columns:
        return False
    if candidate.row_filter is None:
        return member.row_filter is None
    return (
        recorded is not None
        and recorded.get("specHash") == fingerprint(candidate.row_filter)
        and recorded.get("applied") == (member.row_filter or "")
    )


# ---------------------------------------------------------------------------
# teardown
# ---------------------------------------------------------------------------


async def teardown_replication(ctx: ReconcileContext, obj: dict[str, Any]) -> str:
    """Remove what this replication created, whatever its retention policy.

    Works from status - the names and instance recorded when the objects were
    created - not from the dbRefs, whose PostgresDBs may already be gone. The
    replicated data on the target is never touched.
    """
    status = obj.get("status") or {}
    created = status.get("created") or {}
    granted = list(status.get("grantedMemberships") or [])
    source = status.get("source") or {}
    if not (any(created.values()) or granted) or not source.get("instance"):
        return "nothing to remove; this replication created no objects"

    removed: list[str] = []
    if created.get("publication") or created.get("login") or granted:
        instance = await ctx.resolve_instance(str(source["instance"]))
        removed.extend(await _remove_source_objects(ctx, instance, source, created, granted))

    credentials = status.get("credentialsSecret") or {}
    if (
        created.get("secret")
        and credentials.get("name")
        and await delete_secret(ctx.k8s, str(credentials["namespace"]), str(credentials["name"]))
    ):
        removed.append(f"Secret {credentials['namespace']}/{credentials['name']}")
    return "removed " + ", ".join(removed) if removed else "nothing left to remove"


async def _remove_source_objects(
    ctx: ReconcileContext,
    instance: ResolvedInstance,
    source: dict[str, Any],
    created: dict[str, Any],
    granted: list[str],
) -> list[str]:
    removed: list[str] = []
    database = str(source.get("database") or "")
    async with ctx.connect(instance) as conn:
        database_exists = bool(database) and await pgdb.get_database(conn, database) is not None
    if created.get("publication") and database_exists and source.get("publication"):
        async with ctx.connect(instance, database) as conn:
            await pub.drop_publication(conn, str(source["publication"]))
        removed.append(f"publication {source['publication']}")

    login = str(source.get("login") or "")
    async with ctx.connect(instance) as conn:
        if not login or not await pgroles.role_exists(conn, login):
            return removed
        if created.get("login"):
            # Signalling the login's sessions takes membership of it.
            with contextlib.suppress(ReconcileFailure):
                await pgroles.ensure_self_membership(
                    conn, login, per_grant_options=instance.server.supports_per_grant_options
                )
            await pgroles.terminate_role_sessions(conn, login)
            await pgroles.revoke_all_memberships(conn, login)
            await pgroles.drop_role(conn, login)
            removed.append(f"login {login}")
        else:
            # A provided login stays; only what the operator granted it goes.
            for group in granted:
                if group in await pgroles.memberships(conn, login):
                    await pgroles.revoke_membership(conn, group, login)
                    removed.append(f"{group} membership of {login}")
    return removed


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def build_status(
    result: ReplicationResult, existing: dict[str, Any] | None, generation: int
) -> dict[str, Any]:
    conditions = [
        st.reachable(f"source {result.source_server}; target {result.target_server}", generation),
    ]
    held = result.excluded + result.kept
    if held:
        reasons = sorted({entry["reason"] for entry in held})
        reason = reasons[0] if len(reasons) == 1 else "MultipleIssues"
        message = "; ".join(f"{entry['table']}: {entry['message']}" for entry in held[:10])
        if len(held) > 10:
            message += f"; and {len(held) - 10} more in status.excludedTables"
        conditions.append(st.drifted(message, generation, reason=reason))
        conditions.append(st.not_synced(reason, message, generation))
    else:
        conditions.append(st.no_drift(generation))
        conditions.append(st.synced(result.message, generation))
    conditions.append(st.pending("AwaitingSubscription", result.message, generation))
    merged = st.merge_conditions((existing or {}).get("conditions"), conditions)

    published = len(result.published)
    excluded = len({entry["table"] for entry in result.excluded})
    return {
        "observedGeneration": generation,
        "conditions": merged,
        "phase": st.phase_from_conditions(merged),
        "source": {
            **result.source.as_status(),
            "publication": result.names.publication,
            "slot": result.names.subscription,
            "login": result.login,
        },
        "target": {
            **result.target.as_status(),
            "subscription": result.names.subscription,
        },
        "credentialsSecret": result.credentials,
        "created": result.created,
        "grantedMemberships": sorted(set(result.granted)),
        "publishedTables": result.published,
        "excludedTables": result.excluded,
        "tableWarnings": result.kept + result.warnings,
        "rowFilters": result.row_filters,
        "tables": {
            "published": published,
            "excluded": excluded,
            "display": f"{published}/{published + excluded}",
        },
        "lastReconciledAt": st.now(),
    }
