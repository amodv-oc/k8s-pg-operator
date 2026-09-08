"""Read-only view of operator state, backed by the Kubernetes API.

The API container holds no state of its own: every response is projected from
the custom resources' ``status``, which the operator owns. That keeps the API
stateless and independently restartable, and means it can be scaled out for the
frontend without coordinating with the reconciler.

A short TTL cache absorbs dashboard polling without hammering the API server.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from ..config import Settings
from ..constants import API_GROUP, API_VERSION, PLURAL_DATABASE, PLURAL_INSTANCE, PLURAL_USER
from ..k8s.client import K8sClient, is_forbidden, is_not_found
from ..k8s.resources import list_databases, list_instances, list_users
from .schemas import (
    DatabaseSummary,
    GrantSummary,
    GroupRoleSummary,
    Health,
    InstanceSummary,
    Overview,
    PhaseCounts,
    ResourceRef,
    UserSummary,
    conditions_of,
    phase_message,
)
from .schemas import Phase as PhaseLiteral

log = logging.getLogger(__name__)

_KNOWN_PHASES = {
    "Ready",
    "Drifted",
    "Pending",
    "Paused",
    "Failed",
    "Unreachable",
}


class StateStore:
    """Caching projection of the operator's custom resources."""

    def __init__(self, k8s: K8sClient, settings: Settings) -> None:
        self._k8s = k8s
        self._settings = settings
        self._cache: dict[str, tuple[float, Any]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def _cached(self, key: str, loader: Any) -> Any:
        ttl = self._settings.api_cache_ttl
        entry = self._cache.get(key)
        if entry is not None and time.monotonic() - entry[0] < ttl:
            return entry[1]
        # One in-flight load per key, so a burst of dashboard requests does not
        # fan out into a burst of API server calls.
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            entry = self._cache.get(key)
            if entry is not None and time.monotonic() - entry[0] < ttl:
                return entry[1]
            value = await loader()
            self._cache[key] = (time.monotonic(), value)
            return value

    def invalidate(self) -> None:
        self._cache.clear()

    # -- instances ----------------------------------------------------------

    async def instances(self) -> list[InstanceSummary]:
        async def _load() -> list[InstanceSummary]:
            items = await list_instances(self._k8s)
            return [_instance_summary(item) for item in items]

        return await self._cached("instances", _load)

    async def instance(self, name: str) -> InstanceSummary | None:
        for item in await self.instances():
            if item.name == name:
                return item
        return None

    # -- databases ----------------------------------------------------------

    async def databases(self, namespace: str | None = None) -> list[DatabaseSummary]:
        async def _load() -> list[DatabaseSummary]:
            items = await list_databases(self._k8s, namespace)
            return [_database_summary(item) for item in items]

        return await self._cached(f"databases/{namespace or '*'}", _load)

    async def database(self, namespace: str, name: str) -> DatabaseSummary | None:
        for item in await self.databases(namespace):
            if item.name == name:
                return item
        return None

    # -- users --------------------------------------------------------------

    async def users(self, namespace: str | None = None) -> list[UserSummary]:
        async def _load() -> list[UserSummary]:
            items = await list_users(self._k8s, namespace)
            return [_user_summary(item) for item in items]

        return await self._cached(f"users/{namespace or '*'}", _load)

    async def user(self, namespace: str, name: str) -> UserSummary | None:
        for item in await self.users(namespace):
            if item.name == name:
                return item
        return None

    # -- aggregates ---------------------------------------------------------

    async def overview(self) -> Overview:
        instances, databases, users = await asyncio.gather(
            self.instances(), self.databases(), self.users()
        )
        attention: list[str] = []
        for item in instances:
            if item.phase not in {"Ready", "Paused"}:
                attention.append(
                    f"PostgresInstance/{item.name} is {item.phase}"
                    + (f": {item.message}" if item.message else "")
                )
        for db in databases:
            if db.phase not in {"Ready", "Paused"}:
                attention.append(
                    f"PostgresDB/{db.namespace}/{db.name} is {db.phase}"
                    + (f": {db.message}" if db.message else "")
                )
            elif db.orphaned_schemas or db.orphaned_extensions:
                orphans = ", ".join([*db.orphaned_schemas, *db.orphaned_extensions])
                attention.append(
                    f"PostgresDB/{db.namespace}/{db.name} retains orphaned "
                    f"object(s): {orphans}"
                )
        for user in users:
            if user.phase not in {"Ready", "Paused"}:
                attention.append(
                    f"PostgresUser/{user.namespace}/{user.name} is {user.phase}"
                    + (f": {user.message}" if user.message else "")
                )

        from .. import status as st

        return Overview(
            instances=_count_phases(item.phase for item in instances),
            databases=_count_phases(item.phase for item in databases),
            users=_count_phases(item.phase for item in users),
            healthy=not attention,
            attention=attention,
            generated_at=st.now(),
        )

    async def health(self) -> Health:
        """Confirm the API server is reachable and the CRDs are installed."""
        crds: dict[str, bool] = {}
        kubernetes_ok = True
        detail: str | None = None
        for plural in (PLURAL_INSTANCE, PLURAL_DATABASE, PLURAL_USER):
            try:
                await self._k8s.custom.list_cluster_custom_object(
                    group=API_GROUP, version=API_VERSION, plural=plural, limit=1
                )
                crds[plural] = True
            except Exception as exc:  # noqa: BLE001 - reported, not raised
                crds[plural] = False
                if is_not_found(exc):
                    detail = f"CRD {plural}.{API_GROUP} is not installed"
                elif is_forbidden(exc):
                    detail = f"not permitted to list {plural}.{API_GROUP}"
                else:
                    kubernetes_ok = False
                    detail = f"Kubernetes API error listing {plural}: {exc}"

        ok = kubernetes_ok and all(crds.values())
        return Health(
            status="ok" if ok else "degraded",
            kubernetes=kubernetes_ok,
            crds=crds,
            detail=detail,
        )


# ---------------------------------------------------------------------------
# projections
# ---------------------------------------------------------------------------


def _phase(status: dict[str, Any]) -> PhaseLiteral:
    value = str(status.get("phase") or "Unknown")
    return value if value in _KNOWN_PHASES else "Unknown"  # type: ignore[return-value]


def _instance_summary(obj: dict[str, Any]) -> InstanceSummary:
    meta = obj.get("metadata") or {}
    spec = obj.get("spec") or {}
    status = obj.get("status") or {}
    ref = spec.get("credentialsSecretRef") or {}
    return InstanceSummary(
        name=str(meta.get("name")),
        phase=_phase(status),
        endpoint=status.get("endpoint") or _endpoint_from_spec(spec),
        server_version=status.get("serverVersion"),
        server_version_text=status.get("serverVersionText"),
        managing_role=status.get("managingRole"),
        privileges=status.get("privileges"),
        ssl_mode=status.get("sslMode") or spec.get("sslMode"),
        retention_policy=status.get("retentionPolicy") or spec.get("retentionPolicy"),
        credentials_secret=status.get("credentialsSecret")
        or (f"{ref.get('namespace', '')}/{ref.get('name', '')}".strip("/") or None),
        push_secrets_available=status.get("pushSecretsAvailable"),
        databases=int(status.get("databases") or 0),
        users=int(status.get("users") or 0),
        paused=bool(spec.get("paused")),
        last_connected_at=status.get("lastConnectedAt"),
        observed_generation=status.get("observedGeneration"),
        generation=meta.get("generation"),
        creation_timestamp=meta.get("creationTimestamp"),
        conditions=conditions_of(status),
        message=phase_message(status),
    )


def _database_summary(obj: dict[str, Any]) -> DatabaseSummary:
    meta = obj.get("metadata") or {}
    spec = obj.get("spec") or {}
    status = obj.get("status") or {}
    roles = status.get("roles") or {}
    return DatabaseSummary(
        name=str(meta.get("name")),
        namespace=str(meta.get("namespace")),
        phase=_phase(status),
        instance=status.get("instance") or (spec.get("instanceRef") or {}).get("name"),
        endpoint=status.get("endpoint"),
        database_name=status.get("databaseName") or spec.get("databaseName"),
        retention_policy=status.get("retentionPolicy") or spec.get("retentionPolicy"),
        roles=GroupRoleSummary(
            owner=roles.get("owner"),
            read_write=roles.get("readWrite"),
            read_only=roles.get("readOnly"),
        ),
        managed_schemas=_strings(status.get("managedSchemas")),
        observed_schemas=_strings(status.get("observedSchemas")),
        orphaned_schemas=_strings(status.get("orphanedSchemas")),
        unmanaged_schemas=_strings(status.get("unmanagedSchemas")),
        unowned_schemas=_strings(status.get("unownedSchemas")),
        managed_extensions=_strings(status.get("managedExtensions")),
        observed_extensions=_strings(status.get("observedExtensions")),
        orphaned_extensions=_strings(status.get("orphanedExtensions")),
        schema_grants={
            str(key): _strings(value)
            for key, value in (status.get("schemaGrants") or {}).items()
        },
        denied_parameters=_strings(status.get("deniedParameters")),
        paused=bool(spec.get("paused")),
        last_reconciled_at=status.get("lastReconciledAt"),
        observed_generation=status.get("observedGeneration"),
        generation=meta.get("generation"),
        creation_timestamp=meta.get("creationTimestamp"),
        conditions=conditions_of(status),
        message=phase_message(status),
    )


def _user_summary(obj: dict[str, Any]) -> UserSummary:
    meta = obj.get("metadata") or {}
    spec = obj.get("spec") or {}
    status = obj.get("status") or {}
    secret_ref = status.get("secretRef") or {}
    push_ref = status.get("pushSecretRef") or {}
    return UserSummary(
        name=str(meta.get("name")),
        namespace=str(meta.get("namespace")),
        phase=_phase(status),
        instance=status.get("instance") or (spec.get("instanceRef") or {}).get("name"),
        endpoint=status.get("endpoint"),
        username=status.get("username") or spec.get("username"),
        retention_policy=status.get("retentionPolicy") or spec.get("retentionPolicy"),
        grants=[
            GrantSummary(
                database=str(entry.get("database", "")),
                role=str(entry.get("role", "")),
                group_role=str(entry.get("groupRole", "")),
                db_ref=str(entry.get("dbRef", "")),
            )
            for entry in status.get("grants") or []
        ],
        secret_ref=(
            ResourceRef(name=secret_ref["name"], namespace=secret_ref.get("namespace"))
            if secret_ref.get("name")
            else None
        ),
        secret_keys=_strings(status.get("secretKeys")),
        push_secret_ref=(
            ResourceRef(name=push_ref["name"], namespace=push_ref.get("namespace"))
            if push_ref.get("name")
            else None
        ),
        login=bool(spec.get("login", True)),
        paused=bool(spec.get("paused")),
        # Surfaced so an operator can tell at a glance whether the operator or
        # an external system owns this credential. The password is never read.
        byo_password=bool(spec.get("passwordSecretRef")),
        last_reconciled_at=status.get("lastReconciledAt"),
        observed_generation=status.get("observedGeneration"),
        generation=meta.get("generation"),
        creation_timestamp=meta.get("creationTimestamp"),
        conditions=conditions_of(status),
        message=phase_message(status),
    )


def _endpoint_from_spec(spec: dict[str, Any]) -> str | None:
    host = spec.get("host")
    if not host:
        return None
    return f"{host}:{spec.get('port', 5432)}"


def _strings(value: Any) -> list[str]:
    if not value:
        return []
    return [str(entry) for entry in value]


def _count_phases(phases: Any) -> PhaseCounts:
    counts = PhaseCounts()
    for phase in phases:
        counts.total += 1
        match phase:
            case "Ready":
                counts.ready += 1
            case "Drifted":
                counts.drifted += 1
            case "Failed":
                counts.failed += 1
            case "Unreachable":
                counts.unreachable += 1
            case "Pending":
                counts.pending += 1
            case "Paused":
                counts.paused += 1
            case _:
                counts.unknown += 1
    return counts
