"""Shared handler plumbing: error mapping, status writes, metrics.

Every handler funnels through :func:`run_reconcile` so retry semantics, status
conditions, events and metrics behave identically for all three kinds.
"""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

import kopf
from prometheus_client import Counter, Gauge, Histogram

from ..config import get_settings
from ..errors import (
    ConfigurationError,
    ConnectionFailure,
    InstanceNotFound,
    OperatorError,
    ReconcileFailure,
    ReferenceNotFound,
)
from ..k8s.client import is_conflict
from ..k8s.resources import patch_status
from ..reconcile.context import context

log = logging.getLogger(__name__)

RECONCILE_TOTAL = Counter(
    "pg_operator_reconcile_total",
    "Reconcile attempts by kind and outcome.",
    ["kind", "outcome"],
)
RECONCILE_DURATION = Histogram(
    "pg_operator_reconcile_duration_seconds",
    "Reconcile duration by kind.",
    ["kind"],
)
RESOURCE_READY = Gauge(
    "pg_operator_resource_ready",
    "1 when a resource last reconciled successfully, 0 otherwise.",
    ["kind", "namespace", "name"],
)
TEARDOWN_TOTAL = Counter(
    "pg_operator_teardown_total",
    "Finalizer runs by kind and retention policy outcome.",
    ["kind", "outcome"],
)


def as_kopf_error(exc: BaseException) -> Exception:
    """Map an operator error onto kopf's retry semantics.

    ``PermanentError`` stops the retry loop: the spec or a referenced object has
    to change before another attempt can succeed. Everything else is temporary
    and retried with backoff.
    """
    settings = get_settings()
    # ReferenceNotFound is a ConfigurationError subclass and must be tested
    # first: a referenced object may simply not exist *yet*. Applying a
    # PostgresUser and its PostgresDB in one manifest is normal, so treating a
    # missing reference as permanent would leave the user stuck forever.
    if isinstance(exc, ReferenceNotFound):
        return kopf.TemporaryError(str(exc), delay=settings.retry_backoff)
    if isinstance(exc, ConfigurationError):
        return kopf.PermanentError(str(exc))
    if isinstance(exc, ConnectionFailure):
        return kopf.TemporaryError(str(exc), delay=settings.retry_backoff)
    if isinstance(exc, ReconcileFailure | OperatorError):
        return kopf.TemporaryError(str(exc), delay=settings.retry_backoff)
    if is_conflict(exc):
        return kopf.TemporaryError(f"conflicting update: {exc}", delay=5)
    return kopf.TemporaryError(str(exc) or exc.__class__.__name__, delay=settings.retry_backoff)


async def run_reconcile(
    *,
    kind: str,
    plural: str,
    body: kopf.Body,
    logger: Any,
    reconcile: Callable[[], Awaitable[tuple[str, dict[str, Any]]]],
) -> str:
    """Run a reconcile, publish status, emit an event, record metrics.

    ``reconcile`` returns ``(message, status)``. Status is written through the
    status subresource rather than kopf's diff-based patch so that a failure
    still leaves accurate conditions behind.
    """
    meta = body.get("metadata") or {}
    name = str(meta.get("name"))
    namespace = meta.get("namespace")
    labels = (kind, namespace or "-", name)
    ctx = context()
    started = time.monotonic()

    # One pass at a time per resource: the periodic timer and the change
    # handler are independent kopf tasks and would otherwise overlap.
    async with ctx.locks.hold(kind, namespace, name):
        try:
            message, status = await reconcile()
        except Exception as exc:
            RECONCILE_TOTAL.labels(kind=kind, outcome="failure").inc()
            RECONCILE_DURATION.labels(kind=kind).observe(time.monotonic() - started)
            RESOURCE_READY.labels(*labels).set(0)
            logger.error("reconcile failed: %s", exc)
            await _publish_failure(kind, plural, body, exc)
            raise as_kopf_error(exc) from exc

        await patch_status(ctx.k8s, plural, name, status, namespace)
    RECONCILE_TOTAL.labels(kind=kind, outcome="success").inc()
    RECONCILE_DURATION.labels(kind=kind).observe(time.monotonic() - started)
    RESOURCE_READY.labels(*labels).set(1 if status.get("phase") in {"Ready", "Drifted"} else 0)

    if status.get("phase") == "Drifted":
        kopf.event(
            body,
            type="Warning",
            reason="Drifted",
            message=_drift_message(status) or message,
        )
    logger.info(message)
    return message


async def run_teardown(
    *,
    kind: str,
    body: kopf.Body,
    logger: Any,
    teardown: Callable[[], Awaitable[str]],
) -> str:
    """Run a finalizer, mapping errors so a stuck delete is visible, not silent."""
    meta = body.get("metadata") or {}
    name = str(meta.get("name"))
    namespace = meta.get("namespace")
    try:
        async with context().locks.hold(kind, namespace, name):
            message = await teardown()
    except InstanceNotFound as exc:
        # The instance is gone: there is nothing left to act on. Release the
        # finalizer rather than wedging the delete on a resource that can never
        # be cleaned up.
        return _skip_teardown(kind, namespace, name, logger, exc)
    except ReferenceNotFound as exc:
        # Some *other* reference is missing - typically the instance's
        # credentials Secret. That is a temporary condition, and releasing the
        # finalizer over it would leave a DROP-policy role live on the server
        # with nothing left tracking it. Retry instead.
        TEARDOWN_TOTAL.labels(kind=kind, outcome="failure").inc()
        logger.error("cleanup failed: %s", exc)
        raise as_kopf_error(exc) from exc
    except ConfigurationError as exc:
        # The spec cannot be parsed or the namespace is not permitted: no
        # retry can change that, so the delete must be allowed to complete.
        return _skip_teardown(kind, namespace, name, logger, exc)
    except Exception as exc:
        TEARDOWN_TOTAL.labels(kind=kind, outcome="failure").inc()
        logger.error("cleanup failed: %s", exc)
        raise as_kopf_error(exc) from exc

    TEARDOWN_TOTAL.labels(kind=kind, outcome="success").inc()
    _forget_resource_metrics(kind, namespace, name)
    logger.info(message)
    return message


def _skip_teardown(
    kind: str, namespace: str | None, name: str, logger: Any, exc: BaseException
) -> str:
    TEARDOWN_TOTAL.labels(kind=kind, outcome="skipped").inc()
    _forget_resource_metrics(kind, namespace, name)
    logger.warning("releasing finalizer without cleanup: %s", exc)
    return f"cleanup skipped: {exc}"


def _forget_resource_metrics(kind: str, namespace: str | None, name: str) -> None:
    """Drop a deleted resource's per-resource gauge.

    Without this every resource that ever existed stays in the metrics output
    forever, reported as not-ready, and the label cardinality grows unbounded.
    """
    with contextlib.suppress(KeyError):
        RESOURCE_READY.remove(kind, namespace or "-", name)


async def _publish_failure(
    kind: str, plural: str, body: kopf.Body, exc: BaseException
) -> None:
    """Best-effort status write on the failure path."""
    from .. import status as st

    meta = body.get("metadata") or {}
    generation = int(meta.get("generation") or 0)
    reason = type(exc).__name__.removesuffix("Error") or "ReconcileFailed"
    updates = [st.not_ready(reason, str(exc), generation)]
    if isinstance(exc, ConnectionFailure):
        # Without this the Reachable condition keeps saying "Connected" from
        # the last good pass and the phase reads Failed instead of Unreachable,
        # which sends the reader looking for a bug in the spec rather than at
        # the network or the credentials.
        updates = [
            st.not_ready("ConnectionFailed", str(exc), generation),
            st.unreachable("ConnectionFailed", str(exc), generation),
        ]
    conditions = st.merge_conditions(
        (body.get("status") or {}).get("conditions"), updates
    )
    payload = {
        "observedGeneration": generation,
        "conditions": conditions,
        "phase": st.phase_from_conditions(conditions),
        "lastReconciledAt": st.now(),
    }
    try:
        await patch_status(
            context().k8s, plural, str(meta.get("name")), payload, meta.get("namespace")
        )
    except Exception as patch_exc:  # noqa: BLE001 - never mask the original error
        log.warning("could not publish failure status for %s: %s", kind, patch_exc)


def _drift_message(status: dict[str, Any]) -> str:
    for entry in status.get("conditions") or []:
        if entry.get("type") == "Drifted" and entry.get("status") == "True":
            return str(entry.get("message") or "")
    return ""


def is_paused(body: kopf.Body) -> bool:
    return bool((body.get("spec") or {}).get("paused"))


async def publish_paused(kind: str, plural: str, body: kopf.Body, logger: Any) -> str:
    """Record that reconciliation is suspended, and touch nothing else.

    ``spec.paused`` is an escape hatch for taking the operator out of the loop
    on one resource — during a manual migration, or to stop a reconcile loop
    fighting a human. It must therefore short-circuit before any connection is
    opened, which is why it is handled here rather than inside a reconciler.
    """
    from .. import status as st

    meta = body.get("metadata") or {}
    generation = int(meta.get("generation") or 0)
    conditions = st.merge_conditions(
        (body.get("status") or {}).get("conditions"),
        [
            st.condition(
                "Ready",
                "False",
                "Paused",
                "spec.paused is true; reconciliation is suspended",
                observed_generation=generation,
            )
        ],
    )
    payload = {
        "observedGeneration": generation,
        "conditions": conditions,
        "phase": st.phase_from_conditions(conditions),
    }
    await patch_status(
        context().k8s, plural, str(meta.get("name")), payload, meta.get("namespace")
    )
    RESOURCE_READY.labels(kind, meta.get("namespace") or "-", str(meta.get("name"))).set(0)
    logger.info("paused; skipping reconciliation")
    return "paused"
