"""kopf handlers for the namespaced PostgresDB resource."""

from __future__ import annotations

from typing import Any

import kopf

from ..config import get_settings
from ..constants import API_GROUP, API_VERSION, KIND_DATABASE, PLURAL_DATABASE
from ..reconcile.context import context
from ..reconcile.database import build_status, reconcile_database, teardown_database
from .common import is_paused, publish_paused, run_reconcile, run_teardown

_settings = get_settings()


async def _reconcile(body: kopf.Body, logger: Any) -> str:
    if is_paused(body):
        return await publish_paused(KIND_DATABASE, PLURAL_DATABASE, body, logger)
    ctx = context()

    async def _do() -> tuple[str, dict[str, Any]]:
        obj = dict(body)
        generation = int((obj.get("metadata") or {}).get("generation") or 0)
        result, instance = await reconcile_database(ctx, obj)
        status = build_status(result, instance, obj.get("status"), generation)
        return result.message, status

    return await run_reconcile(
        kind=KIND_DATABASE,
        plural=PLURAL_DATABASE,
        body=body,
        logger=logger,
        reconcile=_do,
    )


@kopf.on.create(API_GROUP, API_VERSION, PLURAL_DATABASE)
@kopf.on.update(API_GROUP, API_VERSION, PLURAL_DATABASE)
@kopf.on.resume(API_GROUP, API_VERSION, PLURAL_DATABASE)
async def reconcile(body: kopf.Body, logger: Any, **_: object) -> str:
    """Full convergence pass.

    The same code path handles creation and every later change, so adding a
    schema or an extension to an existing PostgresDB is applied here — including
    the grants and default privileges that make it usable by existing users.
    """
    return await _reconcile(body, logger)


@kopf.timer(
    API_GROUP,
    API_VERSION,
    PLURAL_DATABASE,
    interval=_settings.reconcile_interval,
    idle=_settings.reconcile_idle,
    timeout=_settings.reconcile_timeout,
    sharp=True,
)
async def reconcile_periodically(body: kopf.Body, logger: Any, **_: object) -> str:
    """Correct drift applied directly to the database, outside the operator."""
    return await _reconcile(body, logger)


@kopf.on.delete(API_GROUP, API_VERSION, PLURAL_DATABASE)
async def teardown(body: kopf.Body, logger: Any, **_: object) -> str:
    """Apply the retention policy: RETAIN keeps the database, DROP removes it."""
    ctx = context()

    async def _do() -> str:
        return await teardown_database(ctx, dict(body))

    return await run_teardown(kind=KIND_DATABASE, body=body, logger=logger, teardown=_do)
