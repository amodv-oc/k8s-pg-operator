"""Operator startup and shutdown: kopf configuration and shared clients."""

from __future__ import annotations

import logging

import kopf
from prometheus_client import start_http_server

from ..config import get_settings
from ..constants import FINALIZER
from ..k8s.client import K8sClient
from ..logging_setup import configure_logging
from ..reconcile.context import ReconcileContext, holder

log = logging.getLogger(__name__)


@kopf.on.startup()
async def configure(settings: kopf.OperatorSettings, **_: object) -> None:
    """Configure kopf and build the shared reconcile context."""
    cfg = get_settings()
    configure_logging(cfg.log_level, cfg.log_format)

    settings.persistence.finalizer = FINALIZER
    # Progress and diff-base live in annotations rather than status, so the
    # status subresource stays entirely ours to shape.
    settings.persistence.progress_storage = kopf.AnnotationsProgressStorage(
        prefix="postgres.ourcommunity.com.au"
    )
    settings.persistence.diffbase_storage = kopf.AnnotationsDiffBaseStorage(
        prefix="postgres.ourcommunity.com.au", key="last-handled-configuration"
    )

    # Reconciles talk to an external database; serialising per-resource work
    # keeps one slow instance from starving the rest.
    settings.batching.worker_limit = 8
    settings.batching.idle_timeout = cfg.reconcile_idle
    settings.batching.batch_window = 1.0
    settings.execution.max_workers = 8

    settings.watching.connect_timeout = 60
    settings.watching.server_timeout = 600
    settings.watching.client_timeout = 620

    settings.posting.level = logging.INFO

    k8s = await K8sClient.create()
    holder.set(ReconcileContext(k8s, settings=cfg))

    if cfg.metrics_enabled:
        start_http_server(cfg.metrics_port)
        log.info("serving Prometheus metrics on :%d/metrics", cfg.metrics_port)

    scope = ", ".join(cfg.watch_namespaces) if cfg.watch_namespaces else "all namespaces"
    log.info(
        "pg-operator ready (scope: %s, reconcile interval: %ds, PushSecret: %s)",
        scope,
        cfg.reconcile_interval,
        cfg.pushsecret_api_version if cfg.pushsecret_enabled else "disabled",
    )


@kopf.on.cleanup()
async def shutdown(**_: object) -> None:
    """Close pools and the Kubernetes client on shutdown."""
    try:
        ctx = holder.get()
    except Exception:  # noqa: BLE001 - startup may not have completed
        return
    await ctx.close()
    await ctx.k8s.close()
    holder.clear()
    log.info("pg-operator stopped")


@kopf.on.probe(id="pools")
async def probe_pools(**_: object) -> list[str]:
    """Report open connection pools on the liveness endpoint."""
    try:
        return holder.get().pools.open_pools
    except Exception:  # noqa: BLE001 - probe must never raise
        return []
