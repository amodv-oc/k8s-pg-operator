"""PostgresReplication reconciliation.

The design (``docs/design/replication.md``, "Reconciliation") converges a
replication in eight steps, all of them here: resolve both ends, check both
servers' prerequisites, the replication login, the table set, the publication,
the subscription, health, and status - and the teardown of everything they
create, on both servers.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import Any

from psycopg import AsyncConnection, sql

from .. import status as st
from ..constants import COND_STREAMING, KIND_REPLICATION, PLURAL_REPLICATION
from ..errors import (
    ConfigurationError,
    ConnectionFailure,
    InstanceNotFound,
    InsufficientCapacity,
    InsufficientPrivilege,
    OperatorError,
    ReconcileFailure,
    ReferenceNotFound,
    ReplicationRefused,
    SourceUnreachableFromTarget,
)
from ..k8s.client import K8sClient
from ..k8s.resources import (
    get_database,
    list_databases,
    list_instances,
    list_replications,
    patch_status,
)
from ..k8s.secrets import apply_secret, delete_secret, generate_password, managed_labels, read_secret
from ..models import (
    AlertingSpec,
    DatabaseSpec,
    InstanceSpec,
    LocalCredentialsSecretRef,
    ObjectRef,
    ReplicationSpec,
    parse_database_spec,
    parse_instance_spec,
    parse_replication_spec,
)
from ..naming import ReplicationNames, replication_names
from ..postgres import database as pgdb
from ..postgres import publication as pub
from ..postgres import roles as pgroles
from ..postgres import subscription as sub
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
    REFRESH_HELD_BACK,
    Verdict,
    candidates,
    judge,
    target_relations,
)
from .health import Assessment, Observation, assess

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
    #: Logins this replication used before, each kept in status until it is
    #: removed: ``login``, ``created``, ``grantedMemberships`` and ``secret``.
    superseded: list[dict[str, Any]] = field(default_factory=list)
    published: list[str] = field(default_factory=list)
    excluded: list[dict[str, str]] = field(default_factory=list)
    #: Published tables with a target-side problem, kept published (design D6).
    kept: list[dict[str, str]] = field(default_factory=list)
    #: Kept tables with a partition the subscription does not carry and the
    #: target cannot receive, by table. A refresh would subscribe those too, so
    #: it waits while any is listed here.
    refresh_blockers: dict[str, tuple[str, ...]] = field(default_factory=dict)
    warnings: list[dict[str, str]] = field(default_factory=list)
    row_filters: list[dict[str, str]] = field(default_factory=list)
    changes: list[str] = field(default_factory=list)
    #: The login's password, for the subscription's connection string. Never in status.
    password: str = field(default="", repr=False)
    subscription_oid: int | None = None
    #: The target's, which ends its table-sync slots' names; see sub.sync_slot_pattern.
    target_system_identifier: str | None = None
    subscription_enabled: bool = True
    connection_fingerprint: str = ""
    #: ``schema.table -> srsubstate`` for every table the subscription carries.
    subscribed: dict[str, str] = field(default_factory=dict)
    assessment: Assessment | None = None
    #: Status as this pass last wrote it, so recording patches only a change.
    recorded: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def ready_tables(self) -> int:
        return sum(1 for state in self.subscribed.values() if state == "r")

    @property
    def message(self) -> str:
        summary = (
            f"{self.source.display} -> {self.target.display}: {self.ready_tables} of "
            f"{len(self.subscribed)} subscribed table(s) ready"
        )
        if not self.subscription_enabled:
            summary += "; the subscription is disabled"
        if self.excluded:
            summary += f"; {len({e['table'] for e in self.excluded})} held back"
        if self.changes:
            return f"{summary}; " + "; ".join(self.changes[:6])
        return summary


@dataclass(frozen=True, slots=True)
class Resolved:
    """Step 1's outcome: the spec, and both ends it points at."""

    spec: ReplicationSpec
    source: ReplicationEnd
    target: ReplicationEnd
    source_db: DatabaseSpec
    source_instance: InstanceSpec
    peers: Peers


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
    _refuse_moved_ends(obj.get("status") or {}, source=source, target=target)
    _require_managed_schemas(spec, source, source_db)
    peers = await Peers.read(ctx.k8s, namespace, name)
    await _refuse_cycles(peers, obj, source, target)
    return Resolved(spec, source, target, source_db, source_instance, peers)


def _refuse_moved_ends(
    existing: dict[str, Any], *, source: ReplicationEnd, target: ReplicationEnd
) -> None:
    """Refuse an end that now resolves to a different database from the one in status.

    dbRef is immutable, but what it resolves to is not pinned by it: the
    PostgresDB can be recreated under the same name on another instance (or
    with another databaseName). Converging the new end would create a second
    publication, login and slot there and overwrite status with them, and
    teardown - which works from status alone - would never find the old slot
    again, left retaining WAL on the old source. Refusing keeps status, and
    with it observe_from_status's watch on that slot, exactly as it is.
    """
    if not any((existing.get("created") or {}).values()):
        return
    for side, end in (("source", source), ("target", target)):
        recorded = existing.get(side) or {}
        was = (recorded.get("instance"), recorded.get("database"))
        if not all(was) or was == (end.instance, end.database):
            continue
        raise ConfigurationError(
            f"the {side} PostgresDB {end.namespace}/{end.name} now resolves to "
            f"{end.instance}/{end.database}, but this replication's objects are on "
            f"{was[0]}/{was[1]}; converging would orphan them there. Point the PostgresDB "
            "back, or delete this PostgresReplication - which removes them from where "
            "status records them - and create a new one"
        )


async def reconcile_replication(
    ctx: ReconcileContext, obj: dict[str, Any]
) -> ReplicationResult:
    """Converge a PostgresReplication: the source's login and publication, the target's subscription."""
    meta = obj.get("metadata") or {}
    namespace = str(meta.get("namespace"))
    name = str(meta.get("name"))
    existing = obj.get("status") or {}

    # 1. Resolve both ends and refuse what can never work.
    resolved = await resolve_replication(ctx, obj)
    spec, source, target, source_db = (
        resolved.spec, resolved.source, resolved.target, resolved.source_db
    )
    oid = (existing.get("target") or {}).get("subscriptionOid")
    result = ReplicationResult(
        source=source,
        target=target,
        names=_names(existing, namespace, name, resolved.source_instance.role_prefix),
        # What earlier passes recorded stands until this one learns otherwise,
        # so recording an object early in the pass cannot blank the others.
        login=str((existing.get("source") or {}).get("login") or ""),
        credentials=dict(existing.get("credentialsSecret") or {}),
        created={key: bool(value) for key, value in (existing.get("created") or {}).items()},
        granted=list(existing.get("grantedMemberships") or []),
        superseded=[dict(entry) for entry in existing.get("supersededLogins") or []],
        subscription_oid=int(oid) if oid else None,
        target_system_identifier=(existing.get("target") or {}).get("systemIdentifier"),
        # Normalised as the API server stores it: a key set to None is absent.
        recorded=_merge_patch({}, existing),
    )

    async def record() -> None:
        await _record_created(ctx, obj, result)

    # 2. Prerequisites: both servers new enough, the source logging logically,
    # the target able to own a subscription.
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
    async with (
        ctx.connect(source_instance, source.database) as src,
        ctx.connect(target_instance, target.database) as dst,
    ):
        info = await describe_logical_replication(src, source_instance.server)
        if not info.wal_is_logical:
            (remedy,) = (issue for issue in info.issues() if issue.startswith("wal_level"))
            raise ConfigurationError(f"source {source.display}: {remedy}")
        target_info = await describe_logical_replication(dst, target_instance.server)
        if target_info.create_subscription == "Unavailable":
            raise ConfigurationError(
                f"the managing role on {target.instance} cannot create subscriptions; it needs "
                "membership of pg_create_subscription (or, on RDS, rds_superuser)"
            )

        # Nothing is created until every name is known to be free: a pass that
        # can never succeed must not leave objects behind it. The publication
        # is read here once, and its tables reused by step 5.
        subscription = await sub.get_subscription(dst, result.names.subscription)
        publication = await pub.get_publication(src, result.names.publication)
        await _refuse_foreign_objects(src, spec, result, subscription, publication, marker=marker)
        if subscription is None and not await sub.slots(
            src, result.names.subscription, subscription_oid=None
        ):
            # A slot an earlier pass made is this replication's to adopt, not
            # one more that needs room.
            _require_free_slot(info, source)

        # 3. The publication (empty for now) and the replication login. Each is
        # recorded in status before it is created, so a pass that fails - or
        # stops - part-way cannot hide it from teardown.
        await _ensure_publication(src, spec, result, publication, marker=marker, record=record)
        await _ensure_login(
            ctx, src, spec, info, result,
            namespace=namespace, name=name, marker=marker, read_group=groups["readOnly"],
            record=record,
        )

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
        targets = await pub.describe_target_tables(dst, wanted)
        subscribed = (
            frozenset(await sub.subscribed_tables(dst, subscription.oid))
            if subscription is not None
            else frozenset()
        )
        # Only a table about to get an initial copy needs an empty target; the
        # rest are not even looked at. Each is read as its owner, never as the
        # managing role - see nonempty_tables.
        nonempty = (
            frozenset(
                await pub.nonempty_tables(
                    dst,
                    (
                        relation
                        for relation in targets.values()
                        if relation.qualified not in subscribed
                    ),
                )
            )
            if spec.copy_data
            else frozenset()
        )
        claims = await _claims_elsewhere(resolved.peers, obj, target)
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
                copy_data=spec.copy_data,
                subscribed=subscribed,
                target_nonempty=nonempty,
                source_major_version=source_instance.server.major_version,
            )
            for candidate in selected
        ]

        # 5. The publication's tables: none yet if step 3 just created it.
        current = dict(publication.tables) if publication is not None else {}
        await _converge_tables(src, verdicts, current, existing, result)

        # 6. The subscription.
        published = await pub.published_relations(src, result.names.publication)
        await _converge_subscription(
            ctx, obj, src, dst, spec, result, subscription, published,
            source_instance=source_instance, existing=existing, marker=marker,
        )

        # The subscription now authenticates as the login the spec asks for, so
        # one this replication used before it has nothing left holding it.
        await _remove_superseded_logins(ctx, src, result, instance=source_instance)

        # 7. Health.
        observation = Observation(
            slot=await sub.slot_health(src, result.names.subscription),
            subscription=(
                await sub.subscription_health(dst, result.subscription_oid)
                if result.subscription_oid is not None
                else None
            ),
            tables=result.subscribed,
        )
        result.assessment = assess(
            observation,
            previous=existing.get("health") or {},
            previous_state=existing.get("replicationState"),
            enabled=spec.enabled,
            alerting=spec.alerting,
            # A published table with a target-side problem is the likeliest
            # reason apply has stopped.
            suspects=[f"{entry['table']} ({entry['reason']})" for entry in result.kept],
        )
    return result


async def observe_from_status(ctx: ReconcileContext, obj: dict[str, Any]) -> Assessment | None:
    """Health from what status records, for a pass that failed before step 7.

    A replication whose PostgresDB was deleted under RETAIN no longer resolves,
    but its slot is still on the source pinning WAL - so it is still watched.
    None when there is no slot to watch, or the source cannot be read: saying
    nothing beats reporting a slot lost because it could not be seen.
    """
    status = obj.get("status") or {}
    source = status.get("source") or {}
    target = status.get("target") or {}
    if not (status.get("created") or {}).get("slot") or not source.get("instance") or not source.get("slot"):
        return None
    spec = obj.get("spec") or {}
    try:
        alerting = parse_replication_spec(spec).alerting
    except ValueError:
        alerting = AlertingSpec()

    try:
        instance = await ctx.resolve_instance(str(source["instance"]))
        async with ctx.connect(instance) as conn:
            slot = await sub.slot_health(conn, str(source["slot"]))
    except OperatorError as exc:
        log.debug("cannot observe the source: %s", exc)
        return None

    oid = target.get("subscriptionOid")
    if slot is None and not oid:
        # Recorded just before it was to be made, by a pass that failed first.
        # No subscription has read from it, so nothing is lost: the next pass
        # makes it.
        return None
    subscription, tables = None, {}
    if oid and target.get("instance") and target.get("database"):
        try:
            instance = await ctx.resolve_instance(str(target["instance"]))
            async with ctx.connect(instance, str(target["database"])) as conn:
                subscription = await sub.subscription_health(conn, int(oid))
                tables = await sub.subscribed_tables(conn, int(oid))
        except OperatorError as exc:
            log.debug("cannot observe the target: %s", exc)
    return assess(
        Observation(slot, subscription, tables),
        previous=status.get("health") or {},
        previous_state=status.get("replicationState"),
        enabled=bool(spec.get("enabled", True)),
        alerting=alerting,
        suspects=[],
    )


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


def _rank(meta: dict[str, Any]) -> tuple[str, str, str]:
    """Which of two replications is older: creation time, then namespace and name, so none tie."""
    return (
        str(meta.get("creationTimestamp") or ""),
        str(meta.get("namespace")),
        str(meta.get("name")),
    )


def _streams(obj: dict[str, Any]) -> bool:
    """Whether a replication has a subscription - it replicates, whatever its spec now says.

    ``created.subscription`` is recorded just before the subscription is
    created, so this is also true of one whose CREATE SUBSCRIPTION is still
    failing. That errs the safe way: it keeps its place in the graph, and a
    reverse newcomer waits for it rather than racing it.
    """
    return bool(((obj.get("status") or {}).get("created") or {}).get("subscription"))


@dataclass(slots=True)
class Peers:
    """The other PostgresReplications in the cluster, and where they point, read for one pass.

    PostgresDBs and PostgresInstances are listed once, when first needed, not
    fetched one per replication, so a pass costs the same few reads however
    many replications the cluster holds. It lives for one pass only, so it
    never answers with an end that has since moved. Lookups are ungated:
    ``allowedNamespaces`` governs what a replication may use, not whether
    another one's objects exist.
    """

    k8s: K8sClient
    replications: list[dict[str, Any]]
    _databases: dict[tuple[str, str], dict[str, Any]] | None = None
    _instances: dict[str, dict[str, Any]] | None = None
    _servers: dict[str, tuple[str, int] | None] = field(default_factory=dict)

    @classmethod
    async def read(cls, k8s: K8sClient, namespace: str, name: str) -> Peers:
        """Every PostgresReplication but ``namespace/name``, from one cluster-wide list."""
        others = []
        for other in await list_replications(k8s):
            meta = other.get("metadata") or {}
            if (meta.get("namespace"), meta.get("name")) != (namespace, name):
                others.append(other)
        return cls(k8s, others)

    async def server(self, instance: str) -> tuple[str, int] | None:
        """A PostgresInstance's ``(host, port)``; None if it does not exist or its spec is invalid."""
        if instance not in self._servers:
            if self._instances is None:
                self._instances = {
                    str(obj["metadata"]["name"]): obj for obj in await list_instances(self.k8s)
                }
            found = self._instances.get(instance)
            try:
                spec = parse_instance_spec(found.get("spec") or {}) if found is not None else None
            except ValueError:
                spec = None
            self._servers[instance] = (spec.host.lower(), spec.port) if spec is not None else None
        return self._servers[instance]

    async def database(self, ref: ObjectRef, namespace: str) -> DatabaseIdentity | None:
        """The physical database a dbRef names; None if it cannot be resolved."""
        if self._databases is None:
            self._databases = {
                (str(obj["metadata"].get("namespace")), str(obj["metadata"]["name"])): obj
                for obj in await list_databases(self.k8s)
            }
        found = self._databases.get((ref.resolve_namespace(namespace), ref.name))
        if found is None:
            return None
        try:
            spec = parse_database_spec(found.get("spec") or {})
            database = spec.resolve_database_name(ref.name)
        except ValueError:
            return None
        server = await self.server(spec.instance_ref.name)
        return (*server, database) if server is not None else None


async def _edge(
    peers: Peers, other: dict[str, Any]
) -> tuple[DatabaseIdentity, DatabaseIdentity] | None:
    """The databases another replication runs between; None if it cannot be placed.

    One that streams is placed where status records its objects, because its
    subscription is there whatever has become of its spec: its PostgresDB may
    since have been deleted under RETAIN or recreated elsewhere, or its spec
    made invalid. Any other is placed by resolving its spec.
    """
    status = other.get("status") or {}
    recorded = [status.get(side) or {} for side in ("source", "target")]
    if _streams(other) and all(end.get("instance") and end.get("database") for end in recorded):
        ends = []
        for end in recorded:
            server = await peers.server(str(end["instance"]))
            if server is None:
                return None
            ends.append((*server, str(end["database"])))
        return ends[0], ends[1]
    try:
        spec = parse_replication_spec(other.get("spec") or {})
    except ValueError:
        return None
    namespace = str((other.get("metadata") or {}).get("namespace"))
    source = await peers.database(spec.source.db_ref, namespace)
    target = await peers.database(spec.target.db_ref, namespace)
    return (source, target) if source is not None and target is not None else None


async def _refuse_cycles(
    peers: Peers, obj: dict[str, Any], source: ReplicationEnd, target: ReplicationEnd
) -> None:
    """Refuse a replication that would close a loop of replications.

    A subscription republishes what it applies, so any cycle - not only A->B
    beside B->A, but A->B->C->A - sends each change round until a duplicate key
    stops apply. The graph is of physical databases, so two PostgresDBs naming
    the same database cannot hide a loop between them.

    Which other replications count decides which one a possible loop refuses:

    - One that streams always counts, even while it is being deleted - its
      subscription lasts until its teardown has removed it.
    - One that does not stream yet counts only for a replication that does not
      stream either and is younger, and only while it is not being deleted.

    So a newcomer that would close a loop is refused, never the replication
    already streaming round the rest of it - even if the newcomer is the older
    one, say because it could not resolve until then. Of two that do not stream
    yet, whose passes run concurrently, the younger yields: they cannot both go
    ahead, nor both stand aside. Only a loop that already streams is reported by
    every replication on it.
    """
    meta = obj.get("metadata") or {}
    mine, streaming = _rank(meta), _streams(obj)
    edges: dict[DatabaseIdentity, set[tuple[DatabaseIdentity, str]]] = {}
    for other in peers.replications:
        other_meta = other.get("metadata") or {}
        label = f"{other_meta.get('namespace')}/{other_meta.get('name')}"
        if not _streams(other) and (
            streaming or other_meta.get("deletionTimestamp") or _rank(other_meta) >= mine
        ):
            continue
        edge = await _edge(peers, other)
        if edge is None:
            # Without a subscription, one that cannot resolve cannot be
            # replicating; with one, a PostgresInstance its status names is
            # gone or invalid, so there is no telling which server it is.
            log.debug("ignoring %s in the cycle check: it cannot be placed", label)
            continue
        edges.setdefault(edge[0], set()).add((edge[1], label))

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
    record: Callable[[], Awaitable[None]],
) -> None:
    """A login the target server can stream as, reading through the source's RO group."""
    if spec.source.credentials_secret_ref is not None:
        await _adopt_provided_login(
            ctx, conn, spec.source.credentials_secret_ref, info, result, namespace=namespace
        )
        await record()
    else:
        await _ensure_generated_login(
            ctx, conn, info, result, namespace=namespace, name=name, marker=marker, record=record
        )
    # USAGE, not mere membership: the login must hold the group's SELECTs.
    await _grant(conn, read_group, result.login, result, privilege="USAGE", record=record)


def _take_login(result: ReplicationResult, login: str, *, credentials: dict[str, Any]) -> None:
    """Make ``login`` this replication's login, keeping any login it replaces known.

    ``source.credentialsSecretRef`` can be added to, dropped from or repointed
    at a live replication, which swaps one login for another. The one replaced
    moves to ``supersededLogins`` - whether the operator made it, its Secret,
    what the operator granted it - in the same status patch that records the
    new one, and stays there until it is removed, so neither a pass that fails
    before then nor teardown can lose it.
    """
    generated = bool(credentials.get("generated"))
    if result.login and result.login != login:
        entry: dict[str, Any] = {
            "login": result.login,
            "created": bool(result.created.get("login")),
            "grantedMemberships": sorted(set(result.granted)),
        }
        if result.created.get("secret") and result.credentials.get("name"):
            entry["secret"] = {
                "namespace": result.credentials.get("namespace"),
                "name": result.credentials["name"],
            }
        result.superseded = [e for e in result.superseded if e.get("login") != result.login]
        result.superseded.append(entry)
        # What the operator granted belongs to the login it granted it to.
        result.granted = []
    for entry in [e for e in result.superseded if e.get("login") == login]:
        # A login put back before it was removed: it is this one's again.
        result.superseded.remove(entry)
        result.granted.extend(
            group for group in entry.get("grantedMemberships") or [] if group not in result.granted
        )
    result.login = login
    result.credentials = credentials
    # Recorded false rather than left out for a provided login: status is a
    # merge patch, and a key left out would keep an earlier pass's true - and
    # teardown would drop the user's own login and delete the Secret it read.
    result.created["login"] = generated
    result.created["secret"] = generated


async def _adopt_provided_login(
    ctx: ReconcileContext,
    conn: AsyncConnection,
    ref: LocalCredentialsSecretRef,
    info: LogicalReplicationInfo,
    result: ReplicationResult,
    *,
    namespace: str,
) -> None:
    # The replication's own namespace, never another: the operator reads
    # Secrets cluster-wide, and the reference must not lend that reach out.
    secret_namespace = namespace
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
    privileged = await _privileged(conn, login, managing_role=info.current_user)
    if privileged:
        # This login's password goes into the target's catalog, and to whatever
        # server source.connection names. Neither is a place for a credential
        # that can do more than stream.
        raise ConfigurationError(
            f"credentialsSecretRef {secret_namespace}/{ref.name} names login {login!r}, which is "
            f"{privileged} on {result.source.instance}; a replication login's password is stored "
            "on the target server and sent to source.connection, so provide a dedicated login "
            "that holds REPLICATION (or rds_replication) and nothing more"
        )
    if not role.replication and not await _member_of(conn, login, "rds_replication"):
        raise ConfigurationError(
            f"login {login!r} from credentialsSecretRef holds neither REPLICATION nor "
            "rds_replication, so the target server could not stream as it"
        )
    # Neither the role nor the Secret is the operator's work, whatever an
    # earlier pass with a generated login recorded.
    _take_login(
        result, login, credentials={"namespace": secret_namespace, "name": ref.name, "generated": False}
    )
    result.password = data[ref.password_key].decode()


async def _ensure_generated_login(
    ctx: ReconcileContext,
    conn: AsyncConnection,
    info: LogicalReplicationInfo,
    result: ReplicationResult,
    *,
    namespace: str,
    name: str,
    marker: str,
    record: Callable[[], Awaitable[None]],
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

    # Recorded before the role and its Secret exist, so a pass that fails - or
    # stops - after making them still leaves them to teardown.
    _take_login(
        result, login, credentials={"namespace": secret_namespace, "name": secret_name, "generated": True}
    )
    await record()

    # REPLICATION is named only when it is how streaming is granted. Restating
    # it otherwise would emit NOREPLICATION, which a managing role without
    # REPLICATION - the RDS master user - is refused even as a no-op.
    attributes = {"login": True, "inherit": True}
    if info.replication_grant == "attribute":
        attributes["replication"] = True
    # One transaction, so the role never exists without the marker that makes
    # it this replication's: an unmarked one would be refused as foreign.
    async with conn.transaction():
        created = await pgroles.ensure_login_role(conn, login, password=password, attributes=attributes)
        if created:
            await execute(conn, sql.SQL("COMMENT ON ROLE {} IS {}").format(ident(login), literal(marker)))
    if created:
        result.changes.append(f"created replication login {login}")
    elif stored is None:
        await pgroles.set_password(conn, login, password)
        result.changes.append("credentials Secret was missing; generated and applied a new password")
    result.password = password

    if info.replication_grant == "rds_replication":
        await _grant(conn, "rds_replication", login, result, privilege="MEMBER", record=record)

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


async def _remove_superseded_logins(
    ctx: ReconcileContext, conn: AsyncConnection, result: ReplicationResult, *, instance: ResolvedInstance
) -> None:
    """Remove the logins this replication used before, once nothing streams as them.

    Runs after the subscription carries the new connection string, so the
    session it ends is one nothing reopens. Each stays in status until this
    has run, so a pass that fails first leaves it for the next one, or for
    teardown.
    """
    keep = (result.credentials.get("namespace"), result.credentials.get("name"))
    for entry in list(result.superseded):
        if entry.get("login") != result.login:
            removed = await _remove_login_role(conn, instance, entry)
            removed += await _remove_login_secret(ctx, entry, keep=keep)
            result.changes.extend(f"removed {what}" for what in removed)
        result.superseded.remove(entry)


async def _remove_login_role(
    conn: AsyncConnection, instance: ResolvedInstance, entry: dict[str, Any]
) -> list[str]:
    """A superseded login, removed as far as it is the operator's.

    One the operator made is dropped. A provided one keeps its role, and loses
    only the memberships the operator granted it.
    """
    login = str(entry.get("login") or "")
    if not login or not await pgroles.role_exists(conn, login):
        return []
    if entry.get("created"):
        # Signalling the login's sessions takes membership of it.
        with contextlib.suppress(ReconcileFailure):
            await pgroles.ensure_self_membership(
                conn, login, per_grant_options=instance.server.supports_per_grant_options
            )
        await pgroles.terminate_role_sessions(conn, login)
        await pgroles.revoke_all_memberships(conn, login)
        await pgroles.drop_role(conn, login)
        return [f"superseded login {login}"]
    removed: list[str] = []
    held = await pgroles.memberships(conn, login)
    for group in entry.get("grantedMemberships") or []:
        if group in held:
            await pgroles.revoke_membership(conn, group, login)
            removed.append(f"{group} membership of superseded login {login}")
    return removed


async def _remove_login_secret(
    ctx: ReconcileContext, entry: dict[str, Any], *, keep: tuple[Any, Any] = (None, None)
) -> list[str]:
    """A superseded login's Secret, if the operator wrote it and nothing uses it now."""
    secret = entry.get("secret") or {}
    where = (secret.get("namespace"), secret.get("name"))
    if all(where) and where != keep and await delete_secret(ctx.k8s, str(where[0]), str(where[1])):
        return [f"superseded Secret {where[0]}/{where[1]}"]
    return []


async def _grant(
    conn: AsyncConnection,
    group: str,
    member: str,
    result: ReplicationResult,
    *,
    privilege: str,
    record: Callable[[], Awaitable[None]],
) -> None:
    """Grant membership if missing, then read it back - GRANT only warns when refused.

    A grant is recorded before it is made, so teardown revokes it even if the
    pass stops straight after. A membership the login already held directly
    is not the operator's, and is not recorded.
    """
    has = sql.SQL("SELECT pg_has_role(%s, %s, %s)")
    if await fetch_scalar(conn, has, (member, group, privilege)):
        return
    if group not in result.granted and group not in await pgroles.memberships(conn, member):
        result.granted.append(group)
        await record()
    await pgroles.grant_membership(conn, group, member)
    if not await fetch_scalar(conn, has, (member, group, privilege)):
        raise ReconcileFailure(
            f"GRANT {group} TO {member} was accepted but did not take effect; the managing "
            f"role needs ADMIN OPTION on {group}"
        )
    result.changes.append(f"granted {group} to {member}")


async def _member_of(conn: AsyncConnection, member: str, group: str) -> bool:
    exists = await fetch_scalar(
        conn, sql.SQL("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = %s)"), (group,)
    )
    return bool(exists) and bool(
        await fetch_scalar(conn, sql.SQL("SELECT pg_has_role(%s, %s, 'MEMBER')"), (member, group))
    )


async def _privileged(conn: AsyncConnection, login: str, *, managing_role: str) -> str | None:
    """What makes ``login`` too powerful to be a replication login, if anything does."""
    if login == managing_role:
        return "the managing role"
    if await fetch_scalar(conn, sql.SQL("SELECT rolsuper FROM pg_roles WHERE rolname = %s"), (login,)):
        return "a superuser"
    if await _member_of(conn, login, "rds_superuser"):
        return "a member of rds_superuser"
    return None


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
    peers: Peers, obj: dict[str, Any], target: ReplicationEnd
) -> dict[str, str]:
    """Tables an older replication already publishes into the same target database.

    Two subscriptions writing one table conflict. The older replication keeps
    it - by creation time, then namespace and name - so the two cannot each
    defer to the other and both drop it.
    """
    mine = _rank(obj.get("metadata") or {})
    claims: dict[str, str] = {}
    for other in peers.replications:
        other_meta = other.get("metadata") or {}
        rank = _rank(other_meta)
        if rank >= mine or other_meta.get("deletionTimestamp"):
            continue
        status = other.get("status") or {}
        other_target = status.get("target") or {}
        if other_target.get("database") != target.database or not other_target.get("instance"):
            continue
        if await peers.server(str(other_target["instance"])) != target.server:
            continue
        for table in status.get("publishedTables") or []:
            claims.setdefault(str(table), f"{rank[1]}/{rank[2]}")
    return claims


# ---------------------------------------------------------------------------
# the publication
# ---------------------------------------------------------------------------


async def _refuse_foreign_objects(
    conn: AsyncConnection,
    spec: ReplicationSpec,
    result: ReplicationResult,
    subscription: sub.SubscriptionState | None,
    state: pub.PublicationState | None,
    *,
    marker: str,
) -> None:
    """Refuse to proceed if a name this replication needs belongs to someone else.

    Read-only, and run before anything is created or recorded. An object
    carrying this replication's marker - or recorded in status as created - is
    its own; any other object of that name is never taken over.
    """
    if (
        subscription is not None
        and subscription.comment != marker
        and not result.created.get("subscription")
    ):
        raise ConfigurationError(
            f"a subscription named {subscription.name} already exists in "
            f"{result.target.display} and was not created for this replication (its comment "
            f"is {subscription.comment!r}); the operator will not take it over"
        )
    name = result.names.publication
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
    conn: AsyncConnection,
    spec: ReplicationSpec,
    result: ReplicationResult,
    state: pub.PublicationState | None,
    *,
    marker: str,
    record: Callable[[], Awaitable[None]],
) -> None:
    """Create the publication, or converge its options."""
    name = result.names.publication
    # Recorded before it is created - or as soon as one carrying the marker is
    # adopted - so that from here on teardown removes it.
    result.created["publication"] = True
    await record()
    if state is None:
        await pub.create_publication(
            conn, name, operations=spec.publish, via_root=spec.publish_via_partition_root, comment=marker
        )
        result.changes.append(f"created publication {name}")
        return
    if state.operations != set(spec.publish) or state.via_root != spec.publish_via_partition_root:
        await pub.set_publication_options(
            conn, name, operations=spec.publish, via_root=spec.publish_via_partition_root
        )
        result.changes.append(f"set publish = {', '.join(spec.publish)}")


async def _record_created(ctx: ReconcileContext, obj: dict[str, Any], result: ReplicationResult) -> None:
    """Write what exists, or is about to, to status before the rest of the pass can fail.

    A failed pass publishes only its conditions. Without this, an object made
    early in it would be unknown to teardown - and would outlive a deletion
    that came before the next successful pass. Called *before* each object is
    created, so a pass that stops between creating and recording cannot hide
    it either; teardown passes over a recorded object that does not exist.

    Patches only when the record changes, so a converged pass writes nothing,
    and never writes an empty login or oid: under merge-patch semantics that
    would erase what an earlier pass recorded.
    """
    source = {
        **result.source.as_status(),
        "publication": result.names.publication,
        "slot": result.names.subscription,
    }
    if result.login:
        source["login"] = result.login
    target = {**result.target.as_status(), "subscription": result.names.subscription}
    if result.subscription_oid is not None:
        target["subscriptionOid"] = result.subscription_oid
    if result.target_system_identifier:
        target["systemIdentifier"] = result.target_system_identifier
    patch = copy.deepcopy(
        {
            "source": source,
            "target": target,
            "credentialsSecret": result.credentials,
            "created": result.created,
            "grantedMemberships": sorted(set(result.granted)),
            # None removes the key once nothing is waiting to be removed.
            "supersededLogins": result.superseded or None,
        }
    )
    merged = _merge_patch(result.recorded, patch)
    if merged == result.recorded:
        return
    meta = obj.get("metadata") or {}
    await patch_status(
        ctx.k8s, PLURAL_REPLICATION, str(meta.get("name")), patch, str(meta.get("namespace"))
    )
    result.recorded = merged


def _merge_patch(target: Any, patch: Any) -> Any:
    """What ``target`` becomes under a JSON merge patch (RFC 7386), as the API server applies it."""
    if not isinstance(patch, dict):
        return patch
    merged = dict(target) if isinstance(target, dict) else {}
    for key, value in patch.items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = _merge_patch(merged.get(key), value)
    return merged


async def _converge_tables(
    conn: AsyncConnection,
    verdicts: list[Verdict],
    current: dict[str, pub.PublishedTable],
    existing: dict[str, Any],
    result: ReplicationResult,
) -> None:
    """Make the publication carry exactly the publishable tables, as the spec shapes them.

    ``current`` is the membership as read at the start of the pass. It is read
    back afterwards only if this changed something.
    """
    name = result.names.publication
    changed = False

    recorded = {entry["table"]: entry for entry in existing.get("rowFilters") or []}
    by_table = {verdict.candidate.qualified: verdict for verdict in verdicts}

    for table in sorted(current):
        verdict = by_table.get(table)
        # keeps_membership is false for a table the subscription does not carry:
        # left published, the refresh below would subscribe it afresh - failing
        # on a relation the target lacks, or copying over the rows it holds.
        if verdict is None or not (verdict.publishable or verdict.keeps_membership):
            schema, relation = table.split(".", 1)
            changed = True
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
                if verdict.refresh_blockers:
                    result.refresh_blockers[table] = verdict.refresh_blockers
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
        changed = True
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

    if changed:
        final = await pub.get_publication(conn, name)
        published = final.tables if final is not None else {}
    else:
        published = current
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
# the subscription
# ---------------------------------------------------------------------------


def _require_free_slot(info: LogicalReplicationInfo, source: ReplicationEnd) -> None:
    """A new subscription needs a slot on the source: say so before trying.

    Its initial copy briefly wants more - a table-sync slot per sync worker -
    but PostgreSQL retries those itself, so only the subscription's own slot
    is a precondition.
    """
    if info.max_replication_slots - info.replication_slots_in_use < 1:
        raise InsufficientCapacity(
            f"all {info.max_replication_slots} replication slots on {source.instance} are in "
            "use, so the subscription's slot cannot be created; free one, or raise "
            "max_replication_slots in the parameter group (a static parameter)"
        )


async def _converge_subscription(
    ctx: ReconcileContext,
    obj: dict[str, Any],
    src: AsyncConnection,
    dst: AsyncConnection,
    spec: ReplicationSpec,
    result: ReplicationResult,
    subscription: sub.SubscriptionState | None,
    published: set[str],
    *,
    source_instance: ResolvedInstance,
    existing: dict[str, Any],
    marker: str,
) -> None:
    """Create the subscription, or converge its connection, state, options and tables."""
    name = result.names.subscription
    connection = spec.source.connection
    host = connection.host or source_instance.spec.host
    port = connection.port or source_instance.spec.port
    conninfo = sub.build_conninfo(
        host=host,
        port=port,
        database=result.source.database,
        user=result.login,
        password=result.password,
        ssl_mode=connection.ssl_mode,
        ssl_root_cert=connection.ssl_root_cert_path,
    )
    fingerprint = sub.conninfo_fingerprint(conninfo)
    # Teardown tells this subscription's table-sync slots apart by it, from
    # status: the target may be unreachable by then. Read for a new subscription.
    recorded = (existing.get("target") or {}).get("systemIdentifier") if subscription is not None else None
    result.target_system_identifier = recorded or await sub.get_system_identifier(dst)

    if subscription is None:
        await _ensure_slot(ctx, obj, src, source_instance, result)
        # Recorded before it is created, like the slot.
        result.created["subscription"] = True
        await _record_created(ctx, obj, result)
        try:
            await sub.create_subscription(
                dst,
                name,
                conninfo=conninfo,
                publication=result.names.publication,
                copy_data=spec.copy_data,
                enabled=spec.enabled,
                binary=spec.subscription.binary,
                streaming=spec.subscription.streaming,
                comment=marker,
            )
        except ReconcileFailure as exc:
            raise _explain(exc, result, host, port) from exc
        subscription = await sub.get_subscription(dst, name)
        if subscription is None:  # pragma: no cover - it was created a statement ago
            raise ReconcileFailure(f"subscription {name} was created but cannot be found")
        result.subscription_oid = subscription.oid
        result.changes.append(f"created subscription {name}")
        # Its oid names the table-sync slots teardown must find.
        await _record_created(ctx, obj, result)
    else:
        result.created["subscription"] = True
        result.subscription_oid = subscription.oid
        # A no-op once recorded; otherwise it was adopted by its marker, or a
        # login swap is about to reach it - recorded before either is acted on.
        await _record_created(ctx, obj, result)
        if existing.get("connectionFingerprint") != fingerprint:
            await sub.set_connection(dst, name, conninfo)
            result.changes.append("updated the subscription's connection")
        if subscription.enabled != spec.enabled:
            await sub.set_enabled(dst, name, spec.enabled)
            result.changes.append(f"{'enabled' if spec.enabled else 'disabled'} the subscription")
        binary = spec.subscription.binary
        streaming = spec.subscription.streaming
        binary = binary if binary is not None and binary != subscription.binary else None
        streaming = streaming if streaming is not None and streaming != subscription.streaming else None
        if binary is not None or streaming is not None:
            await sub.set_options(dst, name, binary=binary, streaming=streaming)
            result.changes.append("updated the subscription's options")
    result.connection_fingerprint = fingerprint
    result.subscription_enabled = spec.enabled

    subscribed = await sub.subscribed_tables(dst, subscription.oid)
    # A disabled subscription cannot refresh; the tables catch up once it is enabled.
    if spec.enabled and set(subscribed) != published and result.refresh_blockers:
        # REFRESH takes everything the publication lists, including a partition
        # of a kept table that the target lacks (the statement fails, and with
        # it the pass) or already holds rows in (they would be copied over).
        # Unpublishing the table instead would stop its other partitions. So
        # the refresh waits for the target, and says so.
        result.warnings.extend(
            {
                "table": table,
                "reason": REFRESH_HELD_BACK,
                "message": (
                    f"the target cannot yet receive {', '.join(relations)}, and refreshing the "
                    "subscription would subscribe it too, so its table list is not refreshed "
                    "until that is fixed there; tables published meanwhile wait with it"
                ),
            }
            for table, relations in sorted(result.refresh_blockers.items())
        )
    elif spec.enabled and set(subscribed) != published:
        try:
            await sub.refresh(dst, name, copy_data=spec.copy_data)
        except ReconcileFailure as exc:
            raise _explain(exc, result, host, port) from exc
        result.changes.append("refreshed the subscription's tables")
        subscribed = await sub.subscribed_tables(dst, subscription.oid)
    result.subscribed = subscribed


def _explain(exc: ReconcileFailure, result: ReplicationResult, host: str, port: int) -> ReconcileFailure:
    """Name what failed when the *target server* connected to the source.

    The operator reached both servers; this is the path between them, so the
    remedy is about that path - not about the operator's own connections.
    """
    text = str(exc)
    if "WAL sender" in text or "replication role" in text:
        return ReplicationRefused(
            f"the source refused {result.login} a replication connection ({text}); on RDS the "
            "login needs rds_replication, on a stock server the REPLICATION attribute"
        )
    if "authentication method requirement" in text:
        # libpq, on the target, declined to answer the server it reached.
        return SourceUnreachableFromTarget(
            f"the server {result.target.instance} reached at {host}:{port} did not ask "
            f"{result.login} to authenticate with SCRAM-SHA-256 ({text}), and the replication "
            "password is sent no other way; check that source.connection names the source "
            "itself, that the source's pg_hba uses scram-sha-256 for this login, and that its "
            "password is stored as a SCRAM verifier (password_encryption = scram-sha-256 when "
            "it was last set)"
        )
    if "could not connect to the publisher" in text and ("SSL" in text or "certificate" in text):
        # The path works; the two ends disagree about TLS. Security groups are
        # not the fix, so do not send the reader there.
        return SourceUnreachableFromTarget(
            f"{result.target.instance} reached the source at {host}:{port} but could not agree "
            f"on TLS ({text}); set source.connection.sslMode to what the source offers - "
            "verify-ca and verify-full also need sslRootCertPath on the target server"
        )
    if "could not connect to the publisher" in text:
        return SourceUnreachableFromTarget(
            f"{result.target.instance} could not connect to the source at {host}:{port} "
            f"({text}); check that the source admits connections from the target server "
            "(security groups, routing, pg_hba), and that source.connection names the source "
            "as the target server resolves it"
        )
    return exc


async def _ensure_slot(
    ctx: ReconcileContext,
    obj: dict[str, Any],
    src: AsyncConnection,
    instance: ResolvedInstance,
    result: ReplicationResult,
) -> None:
    """The subscription's slot, created from the source before the subscription.

    Never by CREATE SUBSCRIPTION itself - see :func:`sub.create_subscription`
    for the cross-server deadlock that avoids. A slot of this name that already
    exists is this replication's own: names carry a hash of its identity. One
    left idle by an earlier pass, or an earlier incarnation, is adopted - its
    position predates the subscription's initial copy, which is all apply
    needs. One in use belongs to some live subscriber and is never touched.
    """
    name = result.names.subscription
    found = await sub.slots(src, name, subscription_oid=None)
    if found:
        if found[0].database != result.source.database:
            raise ReconcileFailure(
                f"replication slot {name} already exists on {result.source.instance} but for "
                f"database {found[0].database!r}, not {result.source.database}; a logical slot "
                "streams one database's changes only, so it cannot be adopted - drop it"
            )
        if found[0].active:
            raise ReconcileFailure(
                f"replication slot {name} already exists on {result.source.instance} and is in "
                f"use (pid {found[0].active_pid}), but this replication has no subscription to "
                "hold it; it is left alone"
            )
    # Recorded before it is created: a slot teardown does not know of pins WAL
    # on the source until its disk fills.
    result.created["slot"] = True
    await _record_created(ctx, obj, result)
    if found:
        result.changes.append(f"adopted idle slot {name}")
    else:
        try:
            async def known_password() -> str | None:
                return result.password

            await _with_slot_privilege(
                ctx,
                instance,
                result.source.database,
                result.login,
                known_password,
                lambda conn: sub.create_slot(conn, name),
            )
        except ReconcileFailure as exc:
            if "statement timeout" not in str(exc):
                raise
            raise ReconcileFailure(
                f"creating slot {name} waits for every transaction already running on "
                f"{result.source.instance} to finish, and one outlasted the statement timeout; "
                "look for long-running transactions in pg_stat_activity there"
            ) from exc
        result.changes.append(f"created slot {name}")


async def _with_slot_privilege(
    ctx: ReconcileContext,
    instance: ResolvedInstance,
    database: str,
    login: str | None,
    credential: Callable[[], Awaitable[str | None]],
    operation: Callable[[AsyncConnection], Awaitable[None]],
) -> None:
    """Run a slot operation as the managing role or, failing that, as the replication login.

    Creating or dropping a slot takes REPLICATION. On RDS the master user has it
    through rds_replication; a stock non-superuser managing role does not, but
    the replication login - which exists to use exactly these slots - does.
    Connected to ``database``, because a logical slot belongs to one database.
    ``credential`` is asked for the login's password only if it is needed.
    """
    async with ctx.connect(instance, database) as conn:
        try:
            await operation(conn)
            return
        except InsufficientPrivilege:
            if not login:
                raise
    password = await credential()
    if not password:
        raise ReplicationRefused(
            f"the managing role may not use replication slots on {instance.name}, and no "
            f"password for {login} is available to do it as the replication login"
        )
    as_login = replace(
        instance.endpoint,
        instance=f"{instance.name}#{login}",
        username=login,
        password=password,
        maintenance_database=database,
    )
    try:
        async with ctx.pools.connection(as_login) as conn:
            await operation(conn)
    except InsufficientPrivilege as exc:
        raise ReplicationRefused(
            f"neither the managing role nor {login} may use replication slots on "
            f"{instance.name} ({exc}); on RDS the login needs rds_replication, on a stock "
            "server the REPLICATION attribute"
        ) from exc
    finally:
        await ctx.pools.close_instance(as_login.instance)


# ---------------------------------------------------------------------------
# teardown
# ---------------------------------------------------------------------------


async def teardown_replication(ctx: ReconcileContext, obj: dict[str, Any]) -> str:
    """Remove what this replication created, on both servers, whatever its retention policy.

    Works from status - the names, instances and objects recorded as they were
    created - not from the dbRefs, whose PostgresDBs may already be gone. The
    replicated data on the target is never touched.

    Each server is handled on its own, because each may be gone, unreachable
    or unusable independently; the finalizer is released only when both are
    done. The target goes first, so nothing consumes the slot by the time it is
    dropped; if the target cannot be cleared, the source is cleared anyway - its
    slot is the part that does damage when left behind.

    Only a PostgresInstance that no longer exists counts as done without being
    cleared. One that exists but cannot be used - its spec no longer parses,
    its credentials Secret is gone - may still have this replication's objects
    on its server, so it holds the finalizer with a retried error naming why.
    Its ``minServerVersion`` and ``allowedNamespaces``, conditions for creating
    objects, do not apply to removing them.
    """
    status = obj.get("status") or {}
    created = status.get("created") or {}
    granted = list(status.get("grantedMemberships") or [])
    source = status.get("source") or {}
    target = status.get("target") or {}
    superseded = list(status.get("supersededLogins") or [])
    if not (any(created.values()) or granted or superseded):
        return "nothing to remove; this replication created no objects"

    removed: list[str] = []
    unreachable: list[str] = []
    failed: list[str] = []

    if created.get("subscription") and target.get("instance") and target.get("subscription"):
        try:
            instance = await ctx.resolve_instance(str(target["instance"]), version_floor=False)
            removed.extend(await _remove_subscription(ctx, instance, target))
        except InstanceNotFound as exc:
            log.warning("target instance gone; its subscription goes with the server: %s", exc)
        except ConnectionFailure as exc:
            unreachable.append(f"target {target['instance']} ({exc})")
        except OperatorError as exc:
            failed.append(f"target {target['instance']} ({exc})")

    source_cleared = False
    if source.get("instance"):
        try:
            instance = await ctx.resolve_instance(str(source["instance"]), version_floor=False)
            if created.get("subscription") or created.get("slot"):
                removed.extend(await _remove_slots(ctx, instance, obj, source, target))
            removed.extend(await _remove_source_objects(ctx, instance, source, created, granted))
            # Logins a swap replaced, by a pass that failed before removing them.
            async with ctx.connect(instance) as conn:
                for entry in superseded:
                    removed.extend(await _remove_login_role(conn, instance, entry))
            source_cleared = True
        except InstanceNotFound as exc:
            log.warning("source instance gone; nothing left on it to remove: %s", exc)
            source_cleared = True
        except ConnectionFailure as exc:
            unreachable.append(f"source {source['instance']} ({exc})")
        except OperatorError as exc:
            failed.append(f"source {source['instance']} ({exc})")

    # The Secret is the login's credential: it goes only once the login has.
    credentials = status.get("credentialsSecret") or {}
    if (
        source_cleared
        and created.get("secret")
        and credentials.get("name")
        and await delete_secret(ctx.k8s, str(credentials["namespace"]), str(credentials["name"]))
    ):
        removed.append(f"Secret {credentials['namespace']}/{credentials['name']}")
    for entry in superseded if source_cleared else []:
        removed.extend(await _remove_login_secret(ctx, entry))

    if unreachable or failed:
        problems = [f"cannot reach {' and '.join(unreachable)}"] if unreachable else []
        if failed:
            problems.append(f"cannot clear {' and '.join(failed)}")
        done = f"; removed so far: {', '.join(removed)}" if removed else ""
        # Retried: never a ConfigurationError, which a finalizer may take for
        # "nothing can ever be cleaned up".
        error = ReconcileFailure if failed else ConnectionFailure
        raise error(f"{'; '.join(problems)}; will retry{done}")
    return "removed " + ", ".join(removed) if removed else "nothing left to remove"


async def _remove_subscription(
    ctx: ReconcileContext, instance: ResolvedInstance, target: dict[str, Any]
) -> list[str]:
    database = str(target.get("database") or "")
    name = str(target["subscription"])
    async with ctx.connect(instance) as conn:
        # DROP DATABASE refuses a database with a subscription, so a database
        # that is gone took no subscription of ours with it unnoticed.
        if not database or await pgdb.get_database(conn, database) is None:
            return []
    async with ctx.connect(instance, database) as conn:
        if await sub.get_subscription(conn, name) is None:
            return []
        took_slot = await sub.drop_subscription(conn, name)
    return [f"subscription {name}" + ("" if took_slot else " (detached from its slot)")]


async def _remove_slots(
    ctx: ReconcileContext,
    instance: ResolvedInstance,
    obj: dict[str, Any],
    source: dict[str, Any],
    target: dict[str, Any],
) -> list[str]:
    """Drop the subscription's slot, and any table-sync slots, still on the source.

    A plain DROP SUBSCRIPTION took them already; this catches what a detached
    one - or an unreachable target - left behind. A walsender still holding a
    slot belongs to a subscriber that is gone or detached, so it is ended.

    A table-sync slot is this replication's only if it carries the target's
    system identifier as well as the subscription's oid: another target server
    can hold a subscription with the same oid. With no identifier recorded, a
    slot of that oid can be told from neither, so it is not touched - and the
    finalizer is held, naming it, until it is gone.
    """
    slot = str(source.get("slot") or "")
    if not slot:
        return []
    oid = target.get("subscriptionOid")
    subscriber = target.get("systemIdentifier")
    login = str(source.get("login") or "")
    database = str(source.get("database") or "")

    async def mine(conn: AsyncConnection) -> tuple[list[sub.SlotState], list[str]]:
        """This replication's slots, and those it cannot say are or are not its own."""
        found = await sub.slots(
            conn, slot, subscription_oid=int(oid) if oid else None, system_identifier=subscriber
        )
        # A table-sync worker connects with the subscription's own connection
        # string, so its slot is in the source database. One in any other
        # database is some other subscription's, whatever its name says - which
        # matters where system identifiers are not unique between targets.
        found = [s for s in found if s.name == slot or not database or s.database == database]
        if not oid or subscriber:
            return found, []
        return [s for s in found if s.name == slot], [s.name for s in found if s.name != slot]

    async with ctx.connect(instance) as conn:
        found, unproven = await mine(conn)
        if any(s.active for s in found) and login:
            # Signalling a backend takes membership of its role.
            with contextlib.suppress(ReconcileFailure):
                await pgroles.ensure_self_membership(
                    conn, login, per_grant_options=instance.server.supports_per_grant_options
                )
        for _ in range(20):
            busy = [s for s in found if s.active and s.active_pid]
            if not busy:
                break
            for s in busy:
                await sub.terminate(conn, int(s.active_pid or 0))
            await asyncio.sleep(0.25)
            found, unproven = await mine(conn)
        held = [s.name for s in found if s.active]
        if held:
            raise ReconcileFailure(
                f"slot(s) {', '.join(held)} on {instance.name} are still held by a walsender "
                "that could not be ended; retrying"
            )
    names = [s.name for s in found]

    async def credential() -> str | None:
        if not ((obj.get("status") or {}).get("created") or {}).get("login"):
            return await _login_password(ctx, obj)
        # The operator's own login, about to be dropped: give it a fresh password
        # rather than trust the Secret's, which may no longer match - a password
        # changed out of band would otherwise leave the slot undroppable.
        password = generate_password()
        async with ctx.connect(instance) as conn:
            await pgroles.set_password(conn, login, password)
        return password

    if names:
        await _with_slot_privilege(
            ctx,
            instance,
            str(source.get("database") or instance.spec.maintenance_database),
            login or None,
            credential,
            lambda conn: sub.drop_slots(conn, names),
        )
    if unproven:
        raise ReconcileFailure(
            f"slot(s) {', '.join(unproven)} on {instance.name} are named as this replication's "
            "table-sync slots would be, but its status records no target system identifier to tell "
            "them from those of another target server's subscription with the same oid, so they "
            "are left alone; drop them by hand if they are this replication's"
        )
    return [f"slot {name}" for name in names]


async def _login_password(ctx: ReconcileContext, obj: dict[str, Any]) -> str | None:
    """The replication login's password, from whichever Secret holds it, if it still exists."""
    status = obj.get("status") or {}
    credentials = status.get("credentialsSecret") or {}
    if not credentials.get("name"):
        return None
    key = "password"
    if not credentials.get("generated"):
        ref = ((obj.get("spec") or {}).get("source") or {}).get("credentialsSecretRef") or {}
        key = str(ref.get("passwordKey") or "password")
    try:
        data = await read_secret(ctx.k8s, str(credentials["namespace"]), str(credentials["name"]))
    except ReferenceNotFound:
        return None
    value = data.get(key)
    return value.decode() if value else None


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
    conditions.append(st.ready(result.message, generation))
    assessment = result.assessment
    if assessment is not None:
        conditions.append(
            st.condition(
                COND_STREAMING,
                "True" if assessment.streaming else "False",
                assessment.state,
                assessment.message,
                observed_generation=generation,
            )
        )
    merged = st.merge_conditions((existing or {}).get("conditions"), conditions)

    excluded = len({entry["table"] for entry in result.excluded})
    subscribed = len(result.subscribed)
    return {
        "observedGeneration": generation,
        "conditions": merged,
        "phase": st.phase_from_conditions(merged),
        "replicationState": assessment.state if assessment else None,
        "health": assessment.health if assessment else None,
        "source": {
            **result.source.as_status(),
            "publication": result.names.publication,
            "slot": result.names.subscription,
            "login": result.login,
        },
        "target": {
            **result.target.as_status(),
            "subscription": result.names.subscription,
            "subscriptionOid": result.subscription_oid,
            "systemIdentifier": result.target_system_identifier,
        },
        "connectionFingerprint": result.connection_fingerprint,
        "credentialsSecret": result.credentials,
        "created": result.created,
        "grantedMemberships": sorted(set(result.granted)),
        "supersededLogins": result.superseded or None,
        "publishedTables": result.published,
        "subscribedTables": [
            {"table": table, "state": state} for table, state in sorted(result.subscribed.items())
        ],
        "excludedTables": result.excluded,
        "tableWarnings": result.kept + result.warnings,
        "rowFilters": result.row_filters,
        "tables": {
            "published": len(result.published),
            "subscribed": subscribed,
            "ready": result.ready_tables,
            "syncing": subscribed - result.ready_tables,
            "excluded": excluded,
            "display": f"{result.ready_tables}/{subscribed}",
        },
        "lastReconciledAt": st.now(),
    }


async def replications_using(ctx: ReconcileContext, namespace: str, name: str) -> list[str]:
    """PostgresReplications whose source or target is the PostgresDB ``namespace/name``.

    Reads raw specs, so a replication whose spec fails validation still counts:
    it may have created a slot or a subscription before it became invalid.
    """
    found: list[str] = []
    for replication in await list_replications(ctx.k8s):
        meta = replication.get("metadata") or {}
        own_namespace = str(meta.get("namespace"))
        spec = replication.get("spec") or {}
        for side in ("source", "target"):
            ref = (spec.get(side) or {}).get("dbRef") or {}
            if ref.get("name") == name and (ref.get("namespace") or own_namespace) == namespace:
                found.append(f"{own_namespace}/{meta.get('name')} ({side})")
    return found
