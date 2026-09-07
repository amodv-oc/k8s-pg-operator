"""kopf handlers for the cluster-scoped PostgresInstance resource."""

from __future__ import annotations

from typing import Any

import kopf

from ..config import get_settings
from ..constants import API_GROUP, API_VERSION, KIND_INSTANCE, PLURAL_INSTANCE
from ..reconcile.context import context
from ..reconcile.instance import build_status, reconcile_instance, teardown_instance
from .common import is_paused, publish_paused, run_reconcile, run_teardown

_settings = get_settings()


@kopf.on.create(API_GROUP, API_VERSION, PLURAL_INSTANCE)
@kopf.on.update(API_GROUP, API_VERSION, PLURAL_INSTANCE)
@kopf.on.resume(API_GROUP, API_VERSION, PLURAL_INSTANCE)
async def reconcile(body: kopf.Body, logger: Any, **_: object) -> str:
    """Validate credentials and publish server capabilities."""
    if is_paused(body):
        return await publish_paused(KIND_INSTANCE, PLURAL_INSTANCE, body, logger)
    ctx = context()
    name = str(body["metadata"]["name"])
    # A changed spec may point at different credentials or a different host.
    ctx.invalidate(name)
    await ctx.pools.close_instance(name)

    async def _do() -> tuple[str, dict[str, Any]]:
        obj = dict(body)
        result = await reconcile_instance(ctx, obj)
        status = build_status(result, obj.get("status"))
        return result.message, status

    return await run_reconcile(
        kind=KIND_INSTANCE,
        plural=PLURAL_INSTANCE,
        body=body,
        logger=logger,
        reconcile=_do,
    )


@kopf.timer(
    API_GROUP,
    API_VERSION,
    PLURAL_INSTANCE,
    interval=_settings.reconcile_interval,
    idle=_settings.reconcile_idle,
    timeout=_settings.reconcile_timeout,
    sharp=True,
)
async def reconcile_periodically(body: kopf.Body, logger: Any, **_: object) -> str:
    """Re-probe connectivity so a rotated Secret or a down instance is noticed."""
    if is_paused(body):
        return await publish_paused(KIND_INSTANCE, PLURAL_INSTANCE, body, logger)
    ctx = context()

    async def _do() -> tuple[str, dict[str, Any]]:
        obj = dict(body)
        result = await reconcile_instance(ctx, obj)
        status = build_status(result, obj.get("status"))
        return result.message, status

    return await run_reconcile(
        kind=KIND_INSTANCE,
        plural=PLURAL_INSTANCE,
        body=body,
        logger=logger,
        reconcile=_do,
    )


@kopf.on.delete(API_GROUP, API_VERSION, PLURAL_INSTANCE)
async def teardown(body: kopf.Body, logger: Any, **_: object) -> str:
    """Release connections. Never touches the external server."""
    ctx = context()

    async def _do() -> str:
        return await teardown_instance(ctx, dict(body))

    return await run_teardown(kind=KIND_INSTANCE, body=body, logger=logger, teardown=_do)
