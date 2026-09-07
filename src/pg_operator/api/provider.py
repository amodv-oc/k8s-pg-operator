"""Lazy Kubernetes client provider for the API container.

Client creation is deferred and retried rather than done once at startup. A
cluster that is briefly unreachable then leaves the container running and
reporting the reason through ``/readyz``, instead of crash-looping with the
error buried in a restarted container's logs — and ``/healthz`` keeps answering,
so liveness stays an honest signal about the process.
"""

from __future__ import annotations

import asyncio
import logging

from ..config import Settings
from ..k8s.client import K8sClient
from .store import StateStore

log = logging.getLogger(__name__)


class ClientProvider:
    """Creates the Kubernetes client and state store on first successful use."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._k8s: K8sClient | None = None
        self._store: StateStore | None = None
        self._error: str | None = None
        self._lock = asyncio.Lock()

    async def store(self) -> StateStore:
        """The state store, creating the client if it does not exist yet."""
        if self._store is not None:
            return self._store
        async with self._lock:
            if self._store is not None:
                return self._store
            try:
                self._k8s = await K8sClient.create()
            except Exception as exc:
                self._error = str(exc)
                log.error("cannot create Kubernetes client: %s", exc)
                raise
            self._error = None
            self._store = StateStore(self._k8s, self._settings)
            log.info("Kubernetes client ready")
            return self._store

    @property
    def last_error(self) -> str | None:
        return self._error

    async def close(self) -> None:
        if self._k8s is not None:
            await self._k8s.close()
            self._k8s = None
        self._store = None
