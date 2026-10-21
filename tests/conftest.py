"""Shared fixtures.

The live-PostgreSQL fixtures point at a server described by ``PGOP_TEST_DSN``.
They mimic RDS: the managing role is a CREATEDB/CREATEROLE member of
``rds_superuser``, not a true superuser, because that is the constraint the
operator actually has to work under.

    docker run -d --name pgop-test -e POSTGRES_PASSWORD=testpw \
      -p 15432:5432 postgres:16-alpine
    export PGOP_TEST_DSN="host=localhost port=15432 user=pgadmin \
      password=adminpw dbname=postgres"

Without that variable the integration tests skip.

Replication needs a second server, which ``make test-setup`` also starts
(``hack/test-servers.sh``). Tests marked ``replication`` additionally need
``PGOP_TEST_TARGET_DSN``, ``PGOP_TEST_SOURCE_FROM_TARGET`` - the source's
address as the target *server* reaches it - and ``PGOP_TEST_SUPERUSER_DSN``,
which is used for nothing but emulating RDS's ``rds_replication``.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import psycopg
import pytest

from pg_operator.config import Settings
from pg_operator.postgres.connection import Endpoint, PoolRegistry

TEST_DSN_ENV = "PGOP_TEST_DSN"
TARGET_DSN_ENV = "PGOP_TEST_TARGET_DSN"
SOURCE_FROM_TARGET_ENV = "PGOP_TEST_SOURCE_FROM_TARGET"
TARGET_FROM_SOURCE_ENV = "PGOP_TEST_TARGET_FROM_SOURCE"
SUPERUSER_DSN_ENV = "PGOP_TEST_SUPERUSER_DSN"
TARGET_SUPERUSER_DSN_ENV = "PGOP_TEST_TARGET_SUPERUSER_DSN"

REPLICATION_ENVS = (
    TEST_DSN_ENV,
    TARGET_DSN_ENV,
    SOURCE_FROM_TARGET_ENV,
    TARGET_FROM_SOURCE_ENV,
    SUPERUSER_DSN_ENV,
    TARGET_SUPERUSER_DSN_ENV,
)

_SHIM = Path(__file__).resolve().parents[1] / "hack" / "rds-replication-shim.sql"


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "replication: needs the two-server harness from `make test-setup`"
    )


def _dsn() -> str | None:
    return os.environ.get(TEST_DSN_ENV) or None


requires_postgres = pytest.mark.skipif(
    _dsn() is None,
    reason=f"set {TEST_DSN_ENV} to run integration tests against a live server",
)

requires_replication_harness = pytest.mark.skipif(
    not all(os.environ.get(name) for name in REPLICATION_ENVS),
    reason=f"set {', '.join(REPLICATION_ENVS)} (make test-setup) to run replication tests",
)


def _parse_dsn(dsn: str) -> dict[str, str]:
    return dict(
        part.split("=", 1) for part in dsn.split() if "=" in part
    )


def _endpoint_from(dsn: str, instance: str) -> Endpoint:
    parts = _parse_dsn(dsn)
    return Endpoint(
        instance=instance,
        host=parts.get("host", "localhost"),
        port=int(parts.get("port", "5432")),
        username=parts.get("user", "postgres"),
        password=parts.get("password", ""),
        maintenance_database=parts.get("dbname", "postgres"),
        ssl_mode=parts.get("sslmode", "disable"),
        connect_timeout=10,
    )


@pytest.fixture(scope="session")
def settings() -> Settings:
    return Settings(operator_namespace="pg-operator", statement_timeout_ms=15_000)


@pytest.fixture
def endpoint(settings: Settings) -> Endpoint:
    dsn = _dsn()
    if dsn is None:
        pytest.skip(f"{TEST_DSN_ENV} is not set")
    return _endpoint_from(dsn, "test")


@pytest.fixture
def target_endpoint() -> Endpoint:
    """The second server: the replication target, as the managing role."""
    dsn = os.environ.get(TARGET_DSN_ENV)
    if not dsn:
        pytest.skip(f"{TARGET_DSN_ENV} is not set")
    return _endpoint_from(dsn, "test-target")


@pytest.fixture
def source_from_target() -> dict[str, str]:
    """``host``/``port`` of the source as the target server resolves it."""
    value = os.environ.get(SOURCE_FROM_TARGET_ENV)
    if not value:
        pytest.skip(f"{SOURCE_FROM_TARGET_ENV} is not set")
    return _parse_dsn(value)


@pytest.fixture
def target_from_source() -> dict[str, str]:
    """``host``/``port`` of the target as the source server resolves it."""
    value = os.environ.get(TARGET_FROM_SOURCE_ENV)
    if not value:
        pytest.skip(f"{TARGET_FROM_SOURCE_ENV} is not set")
    return _parse_dsn(value)


def _superuser_dsn(*, target: bool = False, database: str | None = None) -> str:
    name = TARGET_SUPERUSER_DSN_ENV if target else SUPERUSER_DSN_ENV
    dsn = os.environ.get(name)
    if not dsn:
        pytest.skip(f"{name} is not set")
    return psycopg.conninfo.make_conninfo(dsn, dbname=database) if database else dsn


def emulate_rds_replication(*, target: bool = False) -> None:
    """Give direct LOGIN members of rds_replication the REPLICATION attribute.

    The one superuser step in the suite. RDS lets a login granted
    rds_replication stream from a slot; stock PostgreSQL checks only the
    attribute, which the managing role cannot confer. Runs the same SQL as the
    cluster harness's shim - on the source server, and on the target too when
    a replication runs the other way.
    """
    for dsn in [_superuser_dsn()] + ([_superuser_dsn(target=True)] if target else []):
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(_SHIM.read_text())


def superuser_execute(
    statement: str,
    params: tuple[object, ...] = (),
    *,
    database: str | None = None,
    target: bool = False,
) -> list[tuple]:
    """Run one statement as a server's superuser - for test set-up and cleanup only."""
    dsn = _superuser_dsn(target=target, database=database)
    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        # No parameters means no placeholder parsing, so a LIKE pattern's % is literal.
        cur.execute(statement, params or None)
        return cur.fetchall() if cur.description else []


@pytest.fixture
async def pools(settings: Settings) -> AsyncIterator[PoolRegistry]:
    registry = PoolRegistry(settings)
    try:
        yield registry
    finally:
        await registry.close_all()


@pytest.fixture
async def conn(pools: PoolRegistry, endpoint: Endpoint):
    """A connection to the maintenance database, as the managing role."""
    async with pools.connection(endpoint) as connection:
        yield connection


async def drop_database_hard(
    pools: PoolRegistry, endpoint: Endpoint, name: str, attempts: int = 6
) -> None:
    """Remove a test database, retrying while sessions are still going away.

    Closing a client pool returns once the sockets are closed, but the server
    backend can take a moment to exit — and a backend that appears after the
    session snapshot cannot be signalled without membership in its role. That
    race is a test-harness concern (in production the finalizer simply retries),
    so it is absorbed here rather than left to flake.
    """
    from pg_operator.postgres import database as db
    from pg_operator.postgres import roles as pgroles
    from pg_operator.postgres.server import describe_server

    await pools.close_database(endpoint.instance, name)
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            async with pools.connection(endpoint) as conn:
                if await db.get_database(conn, name) is None:
                    return
                server = await describe_server(conn)
                await db.set_allow_connections(conn, name, False)
                for role in await db.connection_roles(conn, name):
                    with contextlib.suppress(Exception):
                        await pgroles.ensure_self_membership(
                            conn,
                            role,
                            per_grant_options=server.supports_per_grant_options,
                        )
                await db.terminate_connections(conn, name)
                await db.drop_database(conn, name, force=True)
                return
        except Exception as exc:  # noqa: BLE001 - retried below, re-raised at the end
            last = exc
            await asyncio.sleep(0.2 * (attempt + 1))
    raise AssertionError(f"could not drop test database {name}: {last}")


async def drop_roles_hard(
    pools: PoolRegistry, endpoint: Endpoint, names: list[str]
) -> None:
    """Remove test roles, taking the privileges needed to evict their sessions."""
    from pg_operator.postgres import roles as pgroles
    from pg_operator.postgres.server import describe_server

    async with pools.connection(endpoint) as conn:
        server = await describe_server(conn)
        for name in names:
            if not await pgroles.role_exists(conn, name):
                continue
            with contextlib.suppress(Exception):
                await pgroles.ensure_self_membership(
                    conn, name, per_grant_options=server.supports_per_grant_options
                )
            with contextlib.suppress(Exception):
                await pgroles.terminate_role_sessions(conn, name)
            await pgroles.revoke_all_memberships(conn, name)
            await pgroles.drop_role(conn, name)


@pytest.fixture
def unique() -> str:
    """A short unique suffix, so parallel or repeated runs cannot collide."""
    return uuid.uuid4().hex[:8]
