"""Connection management for the external PostgreSQL instances under management.

One pool is kept per (instance, database) pair. Pools are keyed by a fingerprint
of the connection parameters so that rotating the superuser Secret transparently
retires the stale pool on the next reconcile instead of failing forever with
cached credentials.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import tempfile
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

import psycopg
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from ..config import Settings, get_settings
from ..errors import ConnectionFailure

log = logging.getLogger(__name__)

#: Directory for CA bundles materialised from Secrets. Must be writable even
#: with a read-only root filesystem, so the chart mounts an emptyDir here.
_CERT_DIR = Path(os.environ.get("PG_OPERATOR_CERT_DIR", tempfile.gettempdir())) / "pg-operator-ca"


@dataclass(frozen=True, slots=True)
class Endpoint:
    """Everything needed to connect to one instance as the managing superuser."""

    instance: str
    host: str
    port: int
    username: str
    password: str = field(repr=False)
    maintenance_database: str = "postgres"
    ssl_mode: str = "require"
    ssl_root_cert: str | None = None
    connect_timeout: int = 10

    @property
    def fingerprint(self) -> str:
        """Hash of every field that affects the underlying libpq connection."""
        material = "|".join(
            (
                self.host,
                str(self.port),
                self.username,
                self.password,
                self.ssl_mode,
                self.ssl_root_cert or "",
                str(self.connect_timeout),
            )
        )
        return hashlib.sha256(material.encode()).hexdigest()

    def conninfo(self, dbname: str, *, statement_timeout_ms: int, app_name: str) -> str:
        params: dict[str, str] = {
            "host": self.host,
            "port": str(self.port),
            "dbname": dbname,
            "user": self.username,
            "password": self.password,
            "sslmode": self.ssl_mode,
            "connect_timeout": str(self.connect_timeout),
            "application_name": app_name,
            # statement_timeout guards against a reconcile wedging on a lock;
            # lock_timeout keeps DDL from queueing behind long transactions.
            "options": f"-c statement_timeout={statement_timeout_ms} -c lock_timeout=10000",
        }
        if self.ssl_root_cert:
            params["sslrootcert"] = self.ssl_root_cert
        return psycopg.conninfo.make_conninfo(**params)


def materialise_ca_bundle(instance: str, pem: bytes) -> str:
    """Write a CA bundle to disk for libpq's ``sslrootcert`` and return its path.

    Content-addressed, so repeated reconciles of an unchanged bundle are no-ops.
    """
    digest = hashlib.sha256(pem).hexdigest()[:16]
    _CERT_DIR.mkdir(parents=True, exist_ok=True)
    path = _CERT_DIR / f"{instance}-{digest}.pem"
    if not path.exists():
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(pem)
        tmp.chmod(0o600)
        tmp.replace(path)
    return str(path)


class PoolRegistry:
    """Lazily-created, fingerprint-keyed pools for every managed database."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._pools: dict[tuple[str, str], tuple[str, AsyncConnectionPool]] = {}
        self._lock = asyncio.Lock()

    async def _pool(self, endpoint: Endpoint, dbname: str) -> AsyncConnectionPool:
        key = (endpoint.instance, dbname)
        fingerprint = endpoint.fingerprint

        async with self._lock:
            existing = self._pools.get(key)
            if existing is not None:
                cached_fingerprint, pool = existing
                if cached_fingerprint == fingerprint:
                    return pool
                # Connection parameters changed (most often a rotated Secret).
                log.info(
                    "connection parameters changed, recycling pool",
                    extra={"instance": endpoint.instance, "database": dbname},
                )
                del self._pools[key]
                await _close_quietly(pool)

            pool = AsyncConnectionPool(
                conninfo=endpoint.conninfo(
                    dbname,
                    statement_timeout_ms=self._settings.statement_timeout_ms,
                    app_name=self._settings.application_name,
                ),
                min_size=0,
                max_size=self._settings.pool_max_size,
                max_idle=self._settings.pool_max_idle,
                timeout=float(endpoint.connect_timeout),
                # DDL such as CREATE DATABASE cannot run inside a transaction
                # block; explicit `conn.transaction()` is used where atomicity
                # is actually wanted.
                kwargs={"autocommit": True},
                configure=_configure,
                open=False,
                name=f"{endpoint.instance}/{dbname}",
            )
            await pool.open()
            self._pools[key] = (fingerprint, pool)
            return pool

    @contextlib.asynccontextmanager
    async def connection(
        self, endpoint: Endpoint, dbname: str | None = None
    ) -> AsyncIterator[AsyncConnection]:
        """Check out a connection to ``dbname`` (default: the maintenance database)."""
        target = dbname or endpoint.maintenance_database
        pool = await self._pool(endpoint, target)
        try:
            async with pool.connection() as conn:
                yield conn
        except psycopg.OperationalError as exc:
            raise ConnectionFailure(
                f"cannot connect to {endpoint.host}:{endpoint.port}/{target} "
                f"as {endpoint.username}: {_terse(exc)}"
            ) from exc

    async def close_database(self, instance: str, dbname: str) -> None:
        """Drop the pool for one database — required before DROP DATABASE."""
        async with self._lock:
            entry = self._pools.pop((instance, dbname), None)
        if entry is not None:
            await _close_quietly(entry[1])

    async def close_instance(self, instance: str) -> None:
        """Drop every pool belonging to an instance."""
        async with self._lock:
            keys = [key for key in self._pools if key[0] == instance]
            pools = [self._pools.pop(key)[1] for key in keys]
        for pool in pools:
            await _close_quietly(pool)

    async def close_all(self) -> None:
        async with self._lock:
            pools = [pool for _, pool in self._pools.values()]
            self._pools.clear()
        for pool in pools:
            await _close_quietly(pool)

    @property
    def open_pools(self) -> list[str]:
        return [f"{instance}/{db}" for instance, db in sorted(self._pools)]


def _log_notice(diagnostic: psycopg.errors.Diagnostic) -> None:
    """Surface server-side notices, especially privilege warnings.

    ``GRANT`` and ``REVOKE`` emit a WARNING rather than an error when the
    grantor lacks authority ("no privileges could be granted for ..."), so a
    misconfigured managing role would otherwise produce a database with no
    grants and no visible failure.
    """
    severity = (diagnostic.severity_nonlocalized or diagnostic.severity or "").upper()
    message = (diagnostic.message_primary or "").strip()
    if not message:
        return
    if severity in {"WARNING", "ERROR", "FATAL", "PANIC"}:
        log.warning("postgres %s: %s", severity.lower(), message)
    else:
        log.debug("postgres %s: %s", severity.lower(), message)


async def _configure(conn: AsyncConnection) -> None:
    conn.add_notice_handler(_log_notice)


async def _close_quietly(pool: AsyncConnectionPool) -> None:
    # Shutdown must not mask the error that triggered it.
    try:
        await pool.close()
    except Exception:
        log.debug("error while closing pool %s", pool.name, exc_info=True)


def _terse(exc: BaseException) -> str:
    """First line of a libpq error, so logs stay one line per event."""
    return str(exc).strip().splitlines()[0] if str(exc).strip() else exc.__class__.__name__
