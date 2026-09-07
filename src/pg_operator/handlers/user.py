"""kopf handlers for the namespaced PostgresUser resource."""

from __future__ import annotations

from typing import Any

import kopf

from ..config import get_settings
from ..constants import API_GROUP, API_VERSION, KIND_USER, PLURAL_USER
from ..reconcile.context import context
from ..reconcile.user import build_status, reconcile_user, teardown_user
from .common import is_paused, publish_paused, run_reconcile, run_teardown

_settings = get_settings()


async def _reconcile(body: kopf.Body, logger: Any) -> str:
    if is_paused(body):
        return await publish_paused(KIND_USER, PLURAL_USER, body, logger)
    ctx = context()

    async def _do() -> tuple[str, dict[str, Any]]:
        obj = dict(body)
        generation = int((obj.get("metadata") or {}).get("generation") or 0)
        result, instance = await reconcile_user(ctx, obj)
        status = build_status(result, instance, obj.get("status"), generation)
        return result.message, status

    return await run_reconcile(
        kind=KIND_USER, plural=PLURAL_USER, body=body, logger=logger, reconcile=_do
    )


@kopf.on.create(API_GROUP, API_VERSION, PLURAL_USER)
@kopf.on.update(API_GROUP, API_VERSION, PLURAL_USER)
@kopf.on.resume(API_GROUP, API_VERSION, PLURAL_USER)
async def reconcile(body: kopf.Body, logger: Any, **_: object) -> str:
    """Converge the login role, its grants, its Secret and its PushSecret."""
    return await _reconcile(body, logger)


@kopf.timer(
    API_GROUP,
    API_VERSION,
    PLURAL_USER,
    interval=_settings.reconcile_interval,
    idle=_settings.reconcile_idle,
    timeout=_settings.reconcile_timeout,
    sharp=True,
)
async def reconcile_periodically(body: kopf.Body, logger: Any, **_: object) -> str:
    """Re-assert grants, and recreate the credentials Secret if it was deleted."""
    return await _reconcile(body, logger)


@kopf.on.delete(API_GROUP, API_VERSION, PLURAL_USER)
async def teardown(body: kopf.Body, logger: Any, **_: object) -> str:
    """Apply the retention policy: RETAIN keeps the role, DROP removes it."""
    ctx = context()

    async def _do() -> str:
        return await teardown_user(ctx, dict(body))

    return await run_teardown(kind=KIND_USER, body=body, logger=logger, teardown=_do)
