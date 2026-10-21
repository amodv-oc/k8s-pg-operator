"""The two-server harness can do what PostgresReplication will ask of RDS.

There is no replication reconciler yet. These tests drive plain SQL, as the
RDS-like managing role on both servers, through every privileged step the
design in ``docs/design/replication.md`` relies on - so that when the
reconciler arrives, a failure means the reconciler is wrong, not the harness.

The single superuser step is :func:`emulate_rds_replication`, standing in for
RDS's rule that a login granted ``rds_replication`` may stream. Cleanup also
uses the superuser, so a failed test cannot strand a slot that pins WAL.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass

import pytest
from psycopg import AsyncConnection, sql
from psycopg.conninfo import make_conninfo

from pg_operator.k8s.secrets import generate_password
from pg_operator.postgres.connection import Endpoint, PoolRegistry
from pg_operator.postgres.server import describe_server
from pg_operator.postgres.sql import execute, fetch_scalar, ident, literal

from ..conftest import (
    drop_database_hard,
    emulate_rds_replication,
    requires_replication_harness,
    superuser_execute,
)

pytestmark = [requires_replication_harness, pytest.mark.replication, pytest.mark.asyncio]


@dataclass(slots=True)
class Pair:
    """A table on each server, published on the source, plus a replication login."""

    source: Endpoint
    target: Endpoint
    source_from_target: dict[str, str]
    source_db: str
    target_db: str
    login: str
    password: str
    publication: str
    subscription: str

    def conninfo(self, password: str | None = None) -> str:
        return make_conninfo(
            host=self.source_from_target["host"],
            port=self.source_from_target.get("port", "5432"),
            dbname=self.source_db,
            user=self.login,
            password=password or self.password,
            connect_timeout="10",
        )


@pytest.fixture
async def pair(
    pools: PoolRegistry,
    endpoint: Endpoint,
    target_endpoint: Endpoint,
    source_from_target: dict[str, str],
    unique: str,
) -> AsyncIterator[Pair]:
    for side in (endpoint, target_endpoint):
        async with pools.connection(side) as conn:
            server = await describe_server(conn)
            if server.major_version < 16:
                pytest.skip(f"replication needs PostgreSQL 16+, {side.instance} is {server.version}")

    p = Pair(
        source=endpoint,
        target=target_endpoint,
        source_from_target=source_from_target,
        source_db=f"rh_src_{unique}",
        target_db=f"rh_dst_{unique}",
        login=f"rh_repl_{unique}",
        password=generate_password(),
        publication=f"rh_pub_{unique}",
        subscription=f"rh_sub_{unique}",
    )
    table = sql.SQL("CREATE TABLE public.items (id int PRIMARY KEY, label text)")

    async with pools.connection(endpoint) as conn:
        await execute(conn, sql.SQL("CREATE DATABASE {}").format(ident(p.source_db)))
        await execute(
            conn,
            sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                ident(p.login), literal(p.password)
            ),
            describe=f"CREATE ROLE {p.login} LOGIN PASSWORD <redacted>",
        )
    async with pools.connection(endpoint, p.source_db) as conn:
        await execute(conn, table)
        await execute(
            conn, sql.SQL("GRANT SELECT ON public.items TO {}").format(ident(p.login))
        )
        # FOR TABLE needs only ownership of the table - which the managing role
        # has here, and has through <db>_owner under the operator.
        await execute(
            conn,
            sql.SQL("CREATE PUBLICATION {} FOR TABLE public.items").format(
                ident(p.publication)
            ),
        )
    async with pools.connection(target_endpoint) as conn:
        await execute(conn, sql.SQL("CREATE DATABASE {}").format(ident(p.target_db)))
    async with pools.connection(target_endpoint, p.target_db) as conn:
        await execute(conn, table)

    try:
        yield p
    finally:
        await _cleanup(pools, p)


async def _cleanup(pools: PoolRegistry, p: Pair) -> None:
    async with pools.connection(p.target, p.target_db) as conn:
        if await _subscription_exists(conn, p.subscription):
            # Detach from the slot first: dropping it is the source's job below,
            # and must not depend on the target reaching the source.
            for statement in (
                "ALTER SUBSCRIPTION {} DISABLE",
                "ALTER SUBSCRIPTION {} SET (slot_name = NONE)",
                "DROP SUBSCRIPTION {}",
            ):
                await execute(conn, sql.SQL(statement).format(ident(p.subscription)))
    await drop_database_hard(pools, p.target, p.target_db)

    superuser_execute(
        "SELECT pg_terminate_backend(active_pid) FROM pg_replication_slots "
        "WHERE slot_name = %s AND active_pid IS NOT NULL",
        (p.subscription,),
    )
    for _ in range(20):
        if not superuser_execute(
            "SELECT 1 FROM pg_replication_slots WHERE slot_name = %s AND active",
            (p.subscription,),
        ):
            break
        await asyncio.sleep(0.25)
    superuser_execute(
        "SELECT pg_drop_replication_slot(slot_name) FROM pg_replication_slots "
        "WHERE slot_name = %s",
        (p.subscription,),
    )
    await drop_database_hard(pools, p.source, p.source_db)
    superuser_execute(f'DROP ROLE IF EXISTS "{p.login}"')


async def _subscription_exists(conn: AsyncConnection, name: str) -> bool:
    return bool(
        await fetch_scalar(
            conn,
            sql.SQL(
                "SELECT EXISTS (SELECT 1 FROM pg_subscription "
                "WHERE subname = %s AND subdbid = "
                "(SELECT oid FROM pg_database WHERE datname = current_database()))"
            ),
            (name,),
        )
    )


async def _slot(conn: AsyncConnection, name: str) -> tuple[bool, bool] | None:
    """``(exists, active)`` for a slot on the server ``conn`` points at."""
    active = await fetch_scalar(
        conn,
        sql.SQL("SELECT active FROM pg_replication_slots WHERE slot_name = %s"),
        (name,),
    )
    return None if active is None else (True, bool(active))


async def _eventually(check: Callable[[], Awaitable[bool]], what: str) -> None:
    for _ in range(60):
        if await check():
            return
        await asyncio.sleep(0.5)
    raise AssertionError(f"timed out after 30s waiting for {what}")


async def _grant_and_subscribe(pools: PoolRegistry, p: Pair) -> None:
    """The privileged path: grant rds_replication, then subscribe as the managing role."""
    async with pools.connection(p.source) as conn:
        await execute(conn, sql.SQL("GRANT rds_replication TO {}").format(ident(p.login)))
    emulate_rds_replication()
    async with pools.connection(p.target, p.target_db) as conn:
        await execute(
            conn,
            sql.SQL("CREATE SUBSCRIPTION {} CONNECTION {} PUBLICATION {}").format(
                ident(p.subscription), literal(p.conninfo()), ident(p.publication)
            ),
            describe=f"CREATE SUBSCRIPTION {p.subscription} CONNECTION <redacted>",
        )


async def _write_and_await(pools: PoolRegistry, p: Pair, row_id: int, label: str) -> None:
    async with pools.connection(p.source, p.source_db) as conn:
        await execute(
            conn,
            sql.SQL("INSERT INTO public.items VALUES (%s, %s)"),
            (row_id, label),
        )

    async def arrived() -> bool:
        async with pools.connection(p.target, p.target_db) as conn:
            return (
                await fetch_scalar(
                    conn, sql.SQL("SELECT label FROM public.items WHERE id = %s"), (row_id,)
                )
                == label
            )

    await _eventually(arrived, f"row {row_id} on the target")


async def test_rds_replication_is_granted_without_the_managing_role_gaining_it(
    pools, pair
) -> None:
    """The grant works as on RDS, and the emulation reaches only the login."""
    async with pools.connection(pair.source) as conn:
        await execute(conn, sql.SQL("GRANT rds_replication TO {}").format(ident(pair.login)))
        assert await fetch_scalar(
            conn,
            sql.SQL("SELECT pg_has_role(%s, 'rds_replication', 'MEMBER')"),
            (pair.login,),
        )
    emulate_rds_replication()

    async with pools.connection(pair.source) as conn:
        attribute = sql.SQL("SELECT rolreplication FROM pg_roles WHERE rolname = %s")
        assert await fetch_scalar(conn, attribute, (pair.login,)) is True
        # An indirect member through rds_superuser, exactly as the master user
        # is on RDS - and, as there, without the attribute.
        assert await fetch_scalar(conn, attribute, (pair.source.username,)) is False


async def test_rows_stream_from_source_to_target(pools, pair) -> None:
    await _grant_and_subscribe(pools, pair)
    await _write_and_await(pools, pair, 1, "first")

    async with pools.connection(pair.source, pair.source_db) as conn:
        await execute(conn, sql.SQL("UPDATE public.items SET label = 'changed' WHERE id = 1"))
        await execute(conn, sql.SQL("INSERT INTO public.items VALUES (2, 'second')"))
        await execute(conn, sql.SQL("DELETE FROM public.items WHERE id = 2"))

    async def converged() -> bool:
        async with pools.connection(pair.target, pair.target_db) as conn:
            rows = await fetch_scalar(
                conn, sql.SQL("SELECT string_agg(id || '=' || label, ',' ORDER BY id) FROM public.items")
            )
            return rows == "1=changed"

    await _eventually(converged, "the update and delete on the target")


async def test_a_new_password_and_connection_resume_streaming(pools, pair) -> None:
    """The credential self-heal path: ALTER ROLE, then ALTER SUBSCRIPTION CONNECTION.

    On the harness the login carries REPLICATION (set by a superuser), so this
    also proves the managing role may still change its password - on RDS the
    login has only rds_replication membership, and nothing stands in the way.
    """
    await _grant_and_subscribe(pools, pair)
    await _write_and_await(pools, pair, 1, "before")

    rotated = generate_password()
    async with pools.connection(pair.source) as conn:
        await execute(
            conn,
            sql.SQL("ALTER ROLE {} PASSWORD {}").format(ident(pair.login), literal(rotated)),
            describe=f"ALTER ROLE {pair.login} PASSWORD <redacted>",
        )
    async with pools.connection(pair.target, pair.target_db) as conn:
        await execute(
            conn,
            sql.SQL("ALTER SUBSCRIPTION {} CONNECTION {}").format(
                ident(pair.subscription), literal(pair.conninfo(rotated))
            ),
            describe=f"ALTER SUBSCRIPTION {pair.subscription} CONNECTION <redacted>",
        )
    await _write_and_await(pools, pair, 2, "after")


async def test_drop_subscription_removes_the_slot_and_the_login_can_go(pools, pair) -> None:
    """The normal teardown, entirely as the managing role."""
    await _grant_and_subscribe(pools, pair)
    await _write_and_await(pools, pair, 1, "row")
    async with pools.connection(pair.source) as conn:
        assert await _slot(conn, pair.subscription) is not None

    async with pools.connection(pair.target, pair.target_db) as conn:
        await execute(conn, sql.SQL("DROP SUBSCRIPTION {}").format(ident(pair.subscription)))
        # The replicated data is the target's to keep.
        assert await fetch_scalar(conn, sql.SQL("SELECT count(*) FROM public.items")) == 1

    async with pools.connection(pair.source) as conn:
        assert await _slot(conn, pair.subscription) is None
    async with pools.connection(pair.source, pair.source_db) as conn:
        await execute(conn, sql.SQL("DROP PUBLICATION {}").format(ident(pair.publication)))
        await execute(
            conn, sql.SQL("REVOKE SELECT ON public.items FROM {}").format(ident(pair.login))
        )
    async with pools.connection(pair.source) as conn:
        await execute(conn, sql.SQL("REVOKE rds_replication FROM {}").format(ident(pair.login)))
        await execute(conn, sql.SQL("DROP ROLE {}").format(ident(pair.login)))


async def test_a_detached_slot_can_be_dropped_by_the_replication_login(pools, pair) -> None:
    """The fallback teardown, for when the target cannot reach the source.

    ``SET (slot_name = NONE)`` lets the subscription go without touching the
    source, leaving the slot behind. The managing role cannot drop it on a stock
    server (no REPLICATION), but the replication login can - which is the path
    the design takes, and it works on RDS as well.
    """
    await _grant_and_subscribe(pools, pair)
    await _write_and_await(pools, pair, 1, "row")

    async with pools.connection(pair.target, pair.target_db) as conn:
        for statement in (
            "ALTER SUBSCRIPTION {} DISABLE",
            "ALTER SUBSCRIPTION {} SET (slot_name = NONE)",
            "DROP SUBSCRIPTION {}",
        ):
            await execute(conn, sql.SQL(statement).format(ident(pair.subscription)))

    async def inactive() -> bool:
        async with pools.connection(pair.source) as conn:
            return await _slot(conn, pair.subscription) == (True, False)

    # Still there - detached, not dropped - once its walsender has exited.
    await _eventually(inactive, "the walsender to release the slot")

    as_login = Endpoint(
        instance="test-replication-login",
        host=pair.source.host,
        port=pair.source.port,
        username=pair.login,
        password=pair.password,
        maintenance_database=pair.source_db,
        ssl_mode=pair.source.ssl_mode,
    )
    async with pools.connection(as_login) as conn:
        await execute(
            conn,
            sql.SQL("SELECT pg_drop_replication_slot(%s)"),
            (pair.subscription,),
        )
    await pools.close_instance(as_login.instance)

    async with pools.connection(pair.source) as conn:
        assert await _slot(conn, pair.subscription) is None
