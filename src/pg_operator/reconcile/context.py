"""Shared reconcile context: instance resolution, connections, events.

Everything a handler needs to act on a resource hangs off one
:class:`ReconcileContext`, so handlers stay declarative and the plumbing
(credential lookup, pool reuse, server capability detection) lives in one place.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass

from psycopg import AsyncConnection

from ..config import Settings, get_settings
from ..errors import ConfigurationError, ConnectionFailure
from ..k8s.client import K8sClient
from ..k8s.resources import get_instance
from ..k8s.secrets import read_secret_key
from ..models import InstanceSpec, parse_instance_spec
from ..postgres.connection import Endpoint, PoolRegistry, materialise_ca_bundle
from ..postgres.server import ServerInfo, describe_server

log = logging.getLogger(__name__)

#: How long a probed ServerInfo stays valid. Long enough that a reconcile storm
#: does not re-probe, short enough that a minor-version upgrade is picked up.
_SERVER_INFO_TTL = 300.0


@dataclass(frozen=True, slots=True)
class ResolvedInstance:
    """A PostgresInstance resolved to live connection parameters."""

    name: str
    spec: InstanceSpec
    endpoint: Endpoint
    server: ServerInfo
    generation: int

    @property
    def display(self) -> str:
        return f"{self.endpoint.host}:{self.endpoint.port}"


class ResourceLocks:
    """Per-resource mutual exclusion for reconcile and teardown passes.

    kopf runs timers as tasks of their own, independent of the change-handling
    flow, so a periodic pass can start while the create/update handler for the
    same object is still running - or while its finalizer is. Two passes over
    one resource race each other in ways that do not self-heal: both may
    generate a password and only one of them lands in the Secret, both may
    ``CREATE ROLE`` the same name, and the second status patch overwrites the
    first. Holding one lock per resource removes the race without serialising
    unrelated resources - or unrelated instances - against each other.

    Locks are reference-counted so the table does not grow with every resource
    the operator has ever seen.
    """

    def __init__(self) -> None:
        self._locks: dict[tuple[str, str, str], tuple[asyncio.Lock, int]] = {}

    @contextlib.asynccontextmanager
    async def hold(
        self, kind: str, namespace: str | None, name: str
    ) -> AsyncIterator[None]:
        key = (kind, namespace or "", name)
        lock, holders = self._locks.get(key) or (asyncio.Lock(), 0)
        self._locks[key] = (lock, holders + 1)
        try:
            async with lock:
                yield
        finally:
            lock, holders = self._locks[key]
            if holders <= 1:
                del self._locks[key]
            else:
                self._locks[key] = (lock, holders - 1)

    def is_held(self, kind: str, namespace: str | None, name: str) -> bool:
        entry = self._locks.get((kind, namespace or "", name))
        return entry is not None and entry[0].locked()

    def __len__(self) -> int:
        return len(self._locks)


class ReconcileContext:
    """Long-lived collaborators shared by every handler."""

    def __init__(
        self,
        k8s: K8sClient,
        pools: PoolRegistry | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.k8s = k8s
        self.settings = settings or get_settings()
        self.pools = pools or PoolRegistry(self.settings)
        self.locks = ResourceLocks()
        #: Probed capabilities, keyed by (instance, connection fingerprint).
        self._server_info: dict[tuple[str, str], tuple[float, ServerInfo]] = {}

    # -- instance resolution ------------------------------------------------

    async def load_instance_spec(
        self, name: str, *, for_namespace: str | None = None
    ) -> tuple[InstanceSpec, int]:
        """Read and validate a PostgresInstance without connecting to it.

        Enforces the ``allowedNamespaces`` gate. Used on its own by the
        finalizers: a RETAIN teardown has nothing to do on the server, so it
        must not depend on being able to reach it.
        """
        obj = await get_instance(self.k8s, name)
        try:
            spec = parse_instance_spec(obj.get("spec") or {})
        except Exception as exc:
            raise ConfigurationError(
                f"PostgresInstance {name!r} has an invalid spec: {exc}"
            ) from exc
        generation = int((obj.get("metadata") or {}).get("generation") or 0)

        if for_namespace and not spec.permits_namespace(for_namespace):
            raise ConfigurationError(
                f"PostgresInstance {name!r} does not permit namespace "
                f"{for_namespace!r}; allowedNamespaces = {spec.allowed_namespaces}"
            )
        return spec, generation

    async def resolve_instance(
        self, name: str, *, for_namespace: str | None = None
    ) -> ResolvedInstance:
        """Load a PostgresInstance and turn it into a usable endpoint.

        Also enforces the instance's ``allowedNamespaces`` gate and its
        ``minServerVersion`` floor, so a database or user in a namespace that is
        not permitted to use an instance fails with a clear message instead of
        quietly getting objects created.
        """
        spec, generation = await self.load_instance_spec(name, for_namespace=for_namespace)

        endpoint = await self.build_endpoint(name, spec)
        server = await self._describe(endpoint)

        if spec.min_server_version and server.major_version < spec.min_server_version:
            raise ConfigurationError(
                f"PostgresInstance {name!r} requires PostgreSQL "
                f">= {spec.min_server_version} but the server reports {server.version}"
            )

        return ResolvedInstance(
            name=name,
            spec=spec,
            endpoint=endpoint,
            server=server,
            generation=generation,
        )

    async def build_endpoint(self, name: str, spec: InstanceSpec) -> Endpoint:
        """Read the superuser Secret (and CA bundle) into an ``Endpoint``."""
        ref = spec.credentials_secret_ref
        namespace = ref.resolve_namespace(self.settings.operator_namespace)
        username = (
            await read_secret_key(self.k8s, namespace, ref.name, ref.username_key)
        ).decode()
        password = (
            await read_secret_key(self.k8s, namespace, ref.name, ref.password_key)
        ).decode()

        ca_path: str | None = None
        if spec.ssl_root_cert_secret_ref is not None:
            ca_ref = spec.ssl_root_cert_secret_ref
            pem = await read_secret_key(
                self.k8s,
                ca_ref.resolve_namespace(self.settings.operator_namespace),
                ca_ref.name,
                ca_ref.key,
            )
            ca_path = materialise_ca_bundle(name, pem)

        return Endpoint(
            instance=name,
            host=spec.host,
            port=spec.port,
            username=username.strip(),
            password=password,
            maintenance_database=spec.maintenance_database,
            ssl_mode=spec.ssl_mode,
            ssl_root_cert=ca_path,
            connect_timeout=spec.connect_timeout_seconds,
        )

    async def _describe(self, endpoint: Endpoint) -> ServerInfo:
        key = (endpoint.instance, endpoint.fingerprint)
        cached = self._server_info.get(key)
        if cached is not None and time.monotonic() - cached[0] < _SERVER_INFO_TTL:
            return cached[1]
        async with self.pools.connection(endpoint) as conn:
            info = await describe_server(conn)
        if not (info.is_superuser or info.is_rds_superuser):
            log.warning(
                "instance %s: managing role %s is neither superuser nor a member of "
                "rds_superuser (privileges: %s); some operations may fail",
                endpoint.instance,
                info.current_user,
                info.describe_privileges(),
            )
        self._server_info[key] = (time.monotonic(), info)
        return info

    def invalidate(self, instance: str) -> None:
        """Forget cached capability data — used when an instance spec changes.

        Scoped to the one instance: a spec change on one server must not force
        every other server to be re-probed.
        """
        for key in [key for key in self._server_info if key[0] == instance]:
            del self._server_info[key]
        log.debug("invalidated cached server info for %s", instance)

    # -- connections --------------------------------------------------------

    @contextlib.asynccontextmanager
    async def connect(
        self, instance: ResolvedInstance, database: str | None = None
    ) -> AsyncIterator[AsyncConnection]:
        """Connect to ``database``, or the instance's maintenance database."""
        async with self.pools.connection(instance.endpoint, database) as conn:
            yield conn

    async def close(self) -> None:
        await self.pools.close_all()


class _ContextHolder:
    """Process-wide context, populated by the operator's startup handler.

    kopf handlers are module-level functions, so the context they need cannot be
    passed in as an argument; this holder keeps the wiring explicit and makes the
    "used before startup" case a clear error rather than an AttributeError.
    """

    def __init__(self) -> None:
        self._context: ReconcileContext | None = None

    def set(self, context: ReconcileContext) -> None:
        self._context = context

    def get(self) -> ReconcileContext:
        if self._context is None:
            raise ConnectionFailure(
                "reconcile context is not initialised; the operator startup "
                "handler has not run"
            )
        return self._context

    def clear(self) -> None:
        self._context = None


holder = _ContextHolder()


def context() -> ReconcileContext:
    return holder.get()
