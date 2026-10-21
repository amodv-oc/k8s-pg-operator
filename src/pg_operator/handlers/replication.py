"""kopf handlers for the namespaced PostgresReplication resource, and its metrics."""

from __future__ import annotations

import contextlib
import logging
import time
from typing import Any, get_args

import kopf
from prometheus_client import Gauge

from ..config import get_settings
from ..constants import API_GROUP, API_VERSION, KIND_REPLICATION, PLURAL_REPLICATION
from ..k8s.resources import patch_status
from ..reconcile.context import ReconcileContext, context
from ..reconcile.health import Assessment, State
from ..reconcile.replication import (
    ReplicationResult,
    build_status,
    observe_from_status,
    reconcile_replication,
    teardown_replication,
)
from .common import is_paused, publish_paused, run_reconcile, run_teardown

log = logging.getLogger(__name__)
_settings = get_settings()

_LABELS = ["namespace", "name"]
REPLICATION_LAG = Gauge(
    "pg_operator_replication_lag_bytes", "WAL the target has not yet confirmed, in bytes.", _LABELS
)
REPLICATION_RETAINED_WAL = Gauge(
    "pg_operator_replication_retained_wal_bytes",
    "WAL the replication's slot keeps on the source, in bytes.",
    _LABELS,
)
REPLICATION_SLOT_ACTIVE = Gauge(
    "pg_operator_replication_slot_active", "1 while a walsender is consuming the slot.", _LABELS
)
REPLICATION_WORKER_RUNNING = Gauge(
    "pg_operator_replication_worker_running", "1 while the target's apply worker runs.", _LABELS
)
REPLICATION_TABLES = Gauge(
    "pg_operator_replication_tables",
    "Tables by standing: published, subscribed, ready, syncing, excluded.",
    [*_LABELS, "state"],
)
REPLICATION_ERRORS = Gauge(
    "pg_operator_replication_errors",
    "PostgreSQL's apply and table-sync error counts. A gauge: they reset with the server's stats.",
    [*_LABELS, "kind"],
)
REPLICATION_STATE = Gauge(
    "pg_operator_replication_state", "1 for the replication's current state, 0 for the rest.", [*_LABELS, "state"]
)
REPLICATION_OBSERVED = Gauge(
    "pg_operator_replication_observed_timestamp_seconds",
    "When health was last observed. Stale means nothing is watching the slot.",
    _LABELS,
)

_STATES: tuple[str, ...] = get_args(State)
_TABLE_STATES = ("published", "subscribed", "ready", "syncing", "excluded")
_ERROR_KINDS = ("apply", "sync")


def publish_metrics(
    namespace: str, name: str, assessment: Assessment, tables: dict[str, int] | None = None
) -> None:
    """Every sample from one observation. A value that could not be read is left out."""
    health = assessment.health
    labels = (namespace, name)
    for gauge, key in (
        (REPLICATION_LAG, "lagBytes"),
        (REPLICATION_RETAINED_WAL, "retainedWalBytes"),
        (REPLICATION_SLOT_ACTIVE, "slotActive"),
        (REPLICATION_WORKER_RUNNING, "workerRunning"),
    ):
        if health.get(key) is not None:
            gauge.labels(*labels).set(float(health[key]))
    for kind, key in zip(_ERROR_KINDS, ("applyErrorCount", "syncErrorCount"), strict=True):
        if health.get(key) is not None:
            REPLICATION_ERRORS.labels(*labels, kind).set(float(health[key]))
    for state in _STATES:
        REPLICATION_STATE.labels(*labels, state).set(1 if state == assessment.state else 0)
    for state, count in (tables or {}).items():
        REPLICATION_TABLES.labels(*labels, state).set(count)
    REPLICATION_OBSERVED.labels(*labels).set(time.time())


def forget_metrics(namespace: str, name: str) -> None:
    """Drop a deleted replication's series, so label cardinality cannot grow without bound."""
    labels = (namespace, name)
    for gauge in (
        REPLICATION_LAG,
        REPLICATION_RETAINED_WAL,
        REPLICATION_SLOT_ACTIVE,
        REPLICATION_WORKER_RUNNING,
        REPLICATION_OBSERVED,
    ):
        with contextlib.suppress(KeyError):
            gauge.remove(*labels)
    for gauge, values in (
        (REPLICATION_STATE, _STATES),
        (REPLICATION_TABLES, _TABLE_STATES),
        (REPLICATION_ERRORS, _ERROR_KINDS),
    ):
        for value in values:
            with contextlib.suppress(KeyError):
                gauge.remove(*labels, value)


def _table_counts(result: ReplicationResult) -> dict[str, int]:
    return {
        "published": len(result.published),
        "subscribed": len(result.subscribed),
        "ready": result.ready_tables,
        "syncing": len(result.subscribed) - result.ready_tables,
        "excluded": len({entry["table"] for entry in result.excluded}),
    }


async def _observe_anyway(ctx: ReconcileContext, obj: dict[str, Any]) -> None:
    """After a failed pass, still record the slot's health from status. Best effort."""
    meta = obj.get("metadata") or {}
    namespace, name = str(meta.get("namespace")), str(meta.get("name"))
    try:
        assessment = await observe_from_status(ctx, obj)
        if assessment is None:
            return
        await patch_status(
            ctx.k8s,
            PLURAL_REPLICATION,
            name,
            {"health": assessment.health, "replicationState": assessment.state},
            namespace,
        )
        publish_metrics(namespace, name, assessment)
    except Exception as exc:  # noqa: BLE001 - never mask the pass's own failure
        log.debug("could not observe %s/%s after a failed pass: %s", namespace, name, exc)


async def _reconcile(body: kopf.Body, logger: Any) -> str:
    if is_paused(body):
        return await publish_paused(KIND_REPLICATION, PLURAL_REPLICATION, body, logger)
    ctx = context()

    async def _do() -> tuple[str, dict[str, Any]]:
        obj = dict(body)
        meta = obj.get("metadata") or {}
        generation = int(meta.get("generation") or 0)
        try:
            result = await reconcile_replication(ctx, obj)
        except Exception:
            await _observe_anyway(ctx, obj)
            raise
        if result.assessment is not None:
            publish_metrics(
                str(meta.get("namespace")), str(meta.get("name")), result.assessment, _table_counts(result)
            )
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
    """Converge the replication end to end, and observe its health."""
    return await _reconcile(body, logger)


@kopf.timer(
    API_GROUP,
    API_VERSION,
    PLURAL_REPLICATION,
    interval=_settings.replication_interval,
    idle=_settings.reconcile_idle,
    timeout=_settings.reconcile_timeout,
    sharp=True,
)
async def reconcile_periodically(body: kopf.Body, logger: Any, **_: object) -> str:
    """Every replicationInterval: lag and a stalled slot are worth knowing within a minute."""
    return await _reconcile(body, logger)


@kopf.on.delete(API_GROUP, API_VERSION, PLURAL_REPLICATION)
async def teardown(body: kopf.Body, logger: Any, **_: object) -> str:
    """Remove the subscription, slots, publication, login and Secret; keep the rows."""
    ctx = context()

    async def _do() -> str:
        return await teardown_replication(ctx, dict(body))

    message = await run_teardown(kind=KIND_REPLICATION, body=body, logger=logger, teardown=_do)
    meta = body.get("metadata") or {}
    forget_metrics(str(meta.get("namespace")), str(meta.get("name")))
    return message
