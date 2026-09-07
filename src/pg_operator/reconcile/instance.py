"""PostgresInstance reconciliation.

An instance owns no PostgreSQL objects of its own — reconciling it means
proving the credentials work, recording what the server can do, and counting the
resources that depend on it. Everything else keys off that.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from .. import status as st
from ..constants import PLURAL_INSTANCE
from ..errors import ConfigurationError, ConnectionFailure, OperatorError
from ..k8s.pushsecret import crd_available
from ..k8s.resources import list_databases, list_users
from ..models import parse_instance_spec
from .context import ReconcileContext

log = logging.getLogger(__name__)


@dataclass(slots=True)
class InstanceResult:
    """Outcome of one instance reconcile, ready to be written to status."""

    reachable: bool
    conditions: list[dict[str, Any]] = field(default_factory=list)
    status: dict[str, Any] = field(default_factory=dict)
    message: str = ""


async def reconcile_instance(
    ctx: ReconcileContext, obj: dict[str, Any]
) -> InstanceResult:
    """Validate connectivity and publish server capabilities on status."""
    meta = obj.get("metadata") or {}
    name = str(meta.get("name"))
    generation = int(meta.get("generation") or 0)

    try:
        spec = parse_instance_spec(obj.get("spec") or {})
    except Exception as exc:
        raise ConfigurationError(f"invalid spec: {exc}") from exc

    ref = spec.credentials_secret_ref
    credentials = (
        f"{ref.resolve_namespace(ctx.settings.operator_namespace)}/{ref.name}"
    )

    try:
        instance = await ctx.resolve_instance(name)
    except (ConnectionFailure, ConfigurationError) as exc:
        conditions = [
            st.unreachable("ConnectionFailed", str(exc), generation),
            st.not_ready("ConnectionFailed", str(exc), generation),
        ]
        return InstanceResult(
            reachable=False,
            conditions=conditions,
            status={
                "observedGeneration": generation,
                "endpoint": f"{spec.host}:{spec.port}",
                "credentialsSecret": credentials,
                "phase": "Unreachable",
            },
            message=str(exc),
        )

    server = instance.server
    databases = await _count_dependents(ctx, name, list_databases)
    users = await _count_dependents(ctx, name, list_users)

    conditions = [
        st.reachable(
            f"connected to PostgreSQL {server.version} as {server.current_user}",
            generation,
        ),
        st.ready(
            f"{databases} database(s) and {users} user(s) reference this instance",
            generation,
        ),
    ]

    warnings: list[str] = []
    if not (server.is_superuser or server.is_rds_superuser):
        warnings.append(
            f"managing role {server.current_user} holds only "
            f"[{server.describe_privileges()}]; database and role creation "
            "requires SUPERUSER, or rds_superuser with CREATEDB and CREATEROLE"
        )
    if not server.can_create_db:
        warnings.append(f"{server.current_user} lacks CREATEDB")
    if not server.can_create_role:
        warnings.append(f"{server.current_user} lacks CREATEROLE")

    push_secrets_available: bool | None = None
    if ctx.settings.pushsecret_enabled:
        push_secrets_available = await _probe_push_secret_crd(ctx)
        if not push_secrets_available:
            warnings.append(
                f"{ctx.settings.pushsecret_api_version} PushSecret CRD is not "
                "installed; users requesting pushSecret will not sync"
            )

    if warnings:
        conditions.append(
            st.not_synced("PrivilegeWarning", "; ".join(warnings), generation)
        )
    else:
        conditions.append(st.synced("credentials and privileges verified", generation))

    instance_status: dict[str, Any] = {
        "observedGeneration": generation,
        "endpoint": f"{spec.host}:{spec.port}",
        "credentialsSecret": credentials,
        "managingRole": server.current_user,
        "serverVersion": server.version,
        "serverVersionText": server.version_text,
        "privileges": server.describe_privileges(),
        "sslMode": spec.ssl_mode,
        "retentionPolicy": spec.retention_policy,
        "databases": databases,
        "users": users,
        "lastConnectedAt": st.now(),
        "phase": "Ready" if not warnings else "Degraded",
    }
    if push_secrets_available is not None:
        instance_status["pushSecretsAvailable"] = push_secrets_available

    return InstanceResult(
        reachable=True,
        conditions=conditions,
        status=instance_status,
        message=f"PostgreSQL {server.version} at {instance.display}",
    )


async def teardown_instance(ctx: ReconcileContext, obj: dict[str, Any]) -> str:
    """Release connections for a deleted instance.

    Deleting a PostgresInstance never touches the server: it is a pointer to
    infrastructure the operator does not own. Dependent PostgresDB and
    PostgresUser resources are left in place and will report the instance as
    missing until it is recreated or they are removed.
    """
    name = str((obj.get("metadata") or {}).get("name"))
    await ctx.pools.close_instance(name)
    ctx.invalidate(name)

    databases = await _count_dependents(ctx, name, list_databases)
    users = await _count_dependents(ctx, name, list_users)
    if databases or users:
        log.warning(
            "PostgresInstance %s deleted while %d database(s) and %d user(s) "
            "still reference it; no PostgreSQL objects were touched",
            name,
            databases,
            users,
        )
        return (
            f"closed connections; {databases} database(s) and {users} user(s) "
            "still reference this instance"
        )
    return "closed connections"


async def _count_dependents(ctx: ReconcileContext, instance: str, lister: Any) -> int:
    items = await lister(ctx.k8s)
    return sum(
        1
        for item in items
        if ((item.get("spec") or {}).get("instanceRef") or {}).get("name") == instance
    )


async def _probe_push_secret_crd(ctx: ReconcileContext) -> bool:
    try:
        return await crd_available(ctx.k8s, ctx.settings.pushsecret_api_version)
    except OperatorError:
        raise
    except Exception as exc:  # noqa: BLE001 - a probe failure is not a reconcile failure
        log.debug("PushSecret CRD probe failed: %s", exc)
        return False


def build_status(
    result: InstanceResult, existing: dict[str, Any] | None
) -> dict[str, Any]:
    """Merge a reconcile result into the resource's existing status."""
    conditions = st.merge_conditions(
        (existing or {}).get("conditions"), result.conditions
    )
    payload = dict(result.status)
    payload["conditions"] = conditions
    payload["phase"] = st.phase_from_conditions(conditions)
    return payload


PLURAL = PLURAL_INSTANCE
