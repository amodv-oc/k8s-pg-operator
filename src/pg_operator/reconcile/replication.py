"""PostgresReplication reconciliation.

The design (``docs/design/replication.md``, "Reconciliation") converges a
replication in eight steps. This module implements the first: resolve both
ends, and refuse a replication that could never work. It opens no connection
and creates nothing, so status reads ``Pending`` until the publication and
subscription steps land. Every later step starts from what this one resolves,
which is why it ships with the resource itself.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from typing import Any

from .. import status as st
from ..errors import ConfigurationError, ReferenceNotFound
from ..k8s.resources import get_database, list_replications
from ..models import (
    DatabaseSpec,
    InstanceSpec,
    ObjectRef,
    ReplicationSpec,
    parse_database_spec,
    parse_replication_spec,
)
from ..naming import ReplicationNames, replication_names
from .context import ReconcileContext

log = logging.getLogger(__name__)

#: A physical database: host, port, database name.
DatabaseIdentity = tuple[str, int, str]


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


@dataclass(frozen=True, slots=True)
class ReplicationResult:
    source: ReplicationEnd
    target: ReplicationEnd
    names: ReplicationNames

    @property
    def message(self) -> str:
        return (
            f"{self.source.display} -> {self.target.display}: references resolved; "
            "this operator version does not create the publication or subscription yet"
        )


async def reconcile_replication(
    ctx: ReconcileContext, obj: dict[str, Any]
) -> ReplicationResult:
    """Resolve and validate a PostgresReplication. Creates nothing yet."""
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

    try:
        names = replication_names(namespace, name, source_instance.role_prefix)
    except ValueError as exc:
        raise ConfigurationError(f"cannot name this replication's objects: {exc}") from exc
    return ReplicationResult(source=source, target=target, names=names)


def _parse_spec(obj: dict[str, Any]) -> ReplicationSpec:
    try:
        return parse_replication_spec(obj.get("spec") or {})
    except ValueError as exc:
        raise ConfigurationError(f"invalid spec: {exc}") from exc


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


async def teardown_replication(ctx: ReconcileContext, obj: dict[str, Any]) -> str:
    """Nothing to remove: this version creates no objects on either server.

    The finalizer is registered now so that every PostgresReplication carries
    one before a version that does create objects - and must remove them - is
    deployed.
    """
    del ctx, obj
    return "nothing to remove; this operator version creates no replication objects"


def build_status(
    result: ReplicationResult, existing: dict[str, Any] | None, generation: int
) -> dict[str, Any]:
    conditions = st.merge_conditions(
        (existing or {}).get("conditions"),
        [st.pending("AwaitingReconciler", result.message, generation)],
    )
    return {
        "observedGeneration": generation,
        "source": {
            **result.source.as_status(),
            "publication": result.names.publication,
            "slot": result.names.subscription,
            "login": result.names.login,
        },
        "target": {
            **result.target.as_status(),
            "subscription": result.names.subscription,
        },
        "conditions": conditions,
        "phase": st.phase_from_conditions(conditions),
        "lastReconciledAt": st.now(),
    }
