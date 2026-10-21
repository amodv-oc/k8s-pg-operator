"""kopf handlers for the namespaced PostgresReplication resource."""

from __future__ import annotations

from typing import Any

import kopf

from ..config import get_settings
from ..constants import API_GROUP, API_VERSION, KIND_REPLICATION, PLURAL_REPLICATION
from ..reconcile.context import context
from ..reconcile.replication import build_status, reconcile_replication, teardown_replication
from .common import is_paused, publish_paused, run_reconcile, run_teardown

_settings = get_settings()


async def _reconcile(body: kopf.Body, logger: Any) -> str:
    if is_paused(body):
        return await publish_paused(KIND_REPLICATION, PLURAL_REPLICATION, body, logger)
    ctx = context()

    async def _do() -> tuple[str, dict[str, Any]]:
        obj = dict(body)
        generation = int((obj.get("metadata") or {}).get("generation") or 0)
        result = await reconcile_replication(ctx, obj)
        status = build_status(result, obj.get("status"), generation)
        return result.message, status

    return await run_reconcile(
        kind=KIND_REPLICATION,
        plural=PLURAL_REPLICATION,
        body=body,
        logger=logger,
        reconcile=_do,
    )


@kopf.on.create(API_GROUP, API_VERSION, PLURAL_REPLICATION)
@kopf.on.update(API_GROUP, API_VERSION, PLURAL_REPLICATION)
@kopf.on.resume(API_GROUP, API_VERSION, PLURAL_REPLICATION)
async def reconcile(body: kopf.Body, logger: Any, **_: object) -> str:
    """Resolve both ends and refuse what can never work; creates nothing yet."""
    return await _reconcile(body, logger)


@kopf.timer(
    API_GROUP,
    API_VERSION,
    PLURAL_REPLICATION,
    interval=_settings.reconcile_interval,
    idle=_settings.reconcile_idle,
    timeout=_settings.reconcile_timeout,
    sharp=True,
)
async def reconcile_periodically(body: kopf.Body, logger: Any, **_: object) -> str:
    """Re-resolve, so a PostgresDB that appears or moves is noticed."""
    return await _reconcile(body, logger)


@kopf.on.delete(API_GROUP, API_VERSION, PLURAL_REPLICATION)
async def teardown(body: kopf.Body, logger: Any, **_: object) -> str:
    """Remove the replication's objects. This version has created none."""
    ctx = context()

    async def _do() -> str:
        return await teardown_replication(ctx, dict(body))

    return await run_teardown(kind=KIND_REPLICATION, body=body, logger=logger, teardown=_do)
