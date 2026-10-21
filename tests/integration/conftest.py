"""A PostgresReplication between the harness's two servers, for the replication suites.

The real reconcilers drive it - PostgresDB for both databases, then
PostgresReplication - as the RDS-like managing role, with tables created as
each database's owner group, exactly as setRoleForOwners makes migrations
create them.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import psycopg
import pytest
from psycopg import sql

from pg_operator.config import Settings
from pg_operator.constants import PLURAL_DATABASE, PLURAL_INSTANCE, PLURAL_REPLICATION
from pg_operator.errors import ReplicationRefused
from pg_operator.naming import group_role_names
from pg_operator.postgres.connection import Endpoint, PoolRegistry
from pg_operator.postgres.server import describe_server
from pg_operator.postgres.sql import execute, fetch_all, fetch_column, fetch_scalar
from pg_operator.reconcile.context import ReconcileContext
from pg_operator.reconcile.database import reconcile_database
from pg_operator.reconcile.replication import (
    build_status,
    reconcile_replication,
    teardown_replication,
)

from ..conftest import drop_database_hard, drop_roles_hard, emulate_rds_replication, superuser_execute
from ..fakes import FakeK8sClient

NS = "team-a"
OPERATOR_NS = "pg-operator"

SOURCE_TABLES = (
    "CREATE TABLE public.orders (id int PRIMARY KEY, region text, total numeric)",
    "CREATE TABLE public.nokey (v text)",
    "CREATE TABLE public.only_source (id int PRIMARY KEY)",
    "CREATE TABLE audit.events (id int PRIMARY KEY, kind text)",
)
TARGET_TABLES = (
    "CREATE TABLE public.orders (id int PRIMARY KEY, region text, total numeric)",
    "CREATE TABLE public.nokey (v text)",
    "CREATE TABLE audit.events (id int PRIMARY KEY, kind text)",
)


@dataclass(slots=True)
class World:
    ctx: ReconcileContext
    k8s: FakeK8sClient
    pools: PoolRegistry
    source: Endpoint
    target: Endpoint
    source_db: str
    target_db: str
    replication: dict[str, Any]
    unique: str = ""
    target_from_source: dict[str, str] = field(default_factory=dict)
    #: True for a replication that runs from the harness's target server to its source.
    reversed: bool = False
    extras: list[World] = field(default_factory=list)
    last_changes: list[str] = field(default_factory=list)

    @property
    def groups(self) -> dict[str, str]:
        return group_role_names(self.source_db)

    @property
    def status(self) -> dict[str, Any]:
        return self.replication["status"]

    async def run(self, *, emulate: bool = True) -> dict[str, Any]:
        """One reconcile pass, with status written back as the handler does.

        On a stock server a login granted rds_replication still cannot stream;
        the harness's shim stands in for RDS there. The first pass that creates
        the login is refused a walsender until the shim has run - exactly as in
        the cluster harness - so it is applied and the pass retried once.
        """
        try:
            return await self._pass()
        except ReplicationRefused:
            if not emulate:
                raise
        emulate_rds_replication(target=self.reversed)
        return await self._pass()

    async def _pass(self) -> dict[str, Any]:
        result = await reconcile_replication(self.ctx, self.replication)
        self.replication["status"] = build_status(result, self.replication.get("status"), 1)
        self.last_changes = result.changes
        return self.replication["status"]

    async def on_source(self, *statements: str, as_owner: bool = False) -> None:
        async with self.pools.connection(self.source, self.source_db) as conn:
            await _run(conn, statements, self.groups["owner"] if as_owner else None)

    async def on_target(self, *statements: str, as_owner: bool = False) -> None:
        owner = group_role_names(self.target_db)["owner"] if as_owner else None
        async with self.pools.connection(self.target, self.target_db) as conn:
            await _run(conn, statements, owner)

    async def target_value(self, query: str) -> Any:
        async with self.pools.connection(self.target, self.target_db) as conn:
            return await fetch_scalar(conn, sql.SQL(query))  # type: ignore[arg-type]

    async def eventually(self, query: str, expected: Any, what: str) -> None:
        """Poll the target until ``query`` returns ``expected``: streaming is asynchronous."""
        last: Any = None
        for _ in range(60):
            last = await self.target_value(query)
            if last == expected:
                return
            await asyncio.sleep(0.5)
        raise AssertionError(f"timed out waiting for {what}: last saw {last!r}, wanted {expected!r}")

    async def publication(self) -> dict[str, tuple[list[str] | None, str | None]]:
        """``schema.table -> (columns, row filter)`` as the server holds them."""
        name = self.status["source"]["publication"]
        async with self.pools.connection(self.source, self.source_db) as conn:
            rows = await fetch_all(
                conn,
                sql.SQL(
                    """
                    SELECT n.nspname || '.' || c.relname, pg_get_expr(pr.prqual, pr.prrelid),
                           (SELECT array_agg(a.attname ORDER BY a.attnum) FROM pg_attribute a
                             WHERE a.attrelid = pr.prrelid AND a.attnum = ANY(pr.prattrs::int2[]))
                      FROM pg_publication p
                      JOIN pg_publication_rel pr ON pr.prpubid = p.oid
                      JOIN pg_class c ON c.oid = pr.prrelid
                      JOIN pg_namespace n ON n.oid = c.relnamespace
                     WHERE p.pubname = %s
                    """
                ),
                (name,),
            )
        return {str(table): (columns, row_filter) for table, row_filter, columns in rows}

    async def source_slots(self) -> list[str]:
        """Every slot on the source that this replication made or could leave behind."""
        async with self.pools.connection(self.source) as conn:
            return await fetch_column(
                conn,
                sql.SQL(
                    "SELECT slot_name FROM pg_replication_slots "
                    r"WHERE slot_name LIKE 'pgop\_%' OR slot_name ~ '^pg_[0-9]+_sync_' ORDER BY 1"
                ),
            )

    def excluded(self) -> dict[str, str]:
        return {e["table"]: e["reason"] for e in self.status["excludedTables"]}

    async def reverse_pair(self, label: str) -> World:
        """A second replication the other way - target server to source server - on databases of its own."""
        source_db, target_db = f"rs_{label}_src_{self.unique}", f"rs_{label}_dst_{self.unique}"
        databases = [
            _database(f"{label}-src", "dst", source_db, retentionPolicy="DROP"),
            _database(f"{label}-dst", "src", target_db, retentionPolicy="DROP"),
        ]
        for obj in databases:
            self.k8s.custom.add(obj, PLURAL_DATABASE)
            await reconcile_database(self.ctx, obj)
        replication = {
            "apiVersion": "postgres.ourcommunity.com.au/v1alpha1",
            "kind": "PostgresReplication",
            "metadata": {"name": f"rs-{label}-{self.unique}", "namespace": NS, "generation": 1},
            "spec": {
                "source": {
                    "dbRef": {"name": f"{label}-src"},
                    "connection": {
                        "host": self.target_from_source["host"],
                        "port": int(self.target_from_source.get("port", "5432")),
                        "sslMode": "disable",
                    },
                },
                "target": {"dbRef": {"name": f"{label}-dst"}},
                "schemas": [{"name": "audit"}],
            },
        }
        self.k8s.custom.add(replication, PLURAL_REPLICATION)
        other = World(
            self.ctx, self.k8s, self.pools, self.target, self.source, source_db, target_db,
            replication, unique=self.unique, reversed=True,
        )
        self.extras.append(other)
        table = "CREATE TABLE audit.events (id int PRIMARY KEY, kind text)"
        await other.on_source(table, as_owner=True)
        await other.on_target(table, as_owner=True)
        return other

    def move_instance(self, name: str, port: int) -> int:
        """Point an instance somewhere else - a closed port makes it unreachable. Returns the old port."""
        stored = self.k8s.custom.get_stored(PLURAL_INSTANCE, "", name)
        assert stored is not None
        previous = int(stored["spec"]["port"])
        stored["spec"]["port"] = port
        self.ctx.invalidate(name)
        return previous


async def _run(conn: psycopg.AsyncConnection, statements: tuple[str, ...], role: str | None) -> None:
    if role:
        await execute(conn, sql.SQL("SET ROLE {}").format(sql.Identifier(role)))
    try:
        for statement in statements:
            await execute(conn, sql.SQL(statement))  # type: ignore[arg-type]
    finally:
        if role:
            await execute(conn, sql.SQL("RESET ROLE"))


def _instance(name: str, endpoint: Endpoint) -> dict[str, Any]:
    return {
        "apiVersion": "postgres.ourcommunity.com.au/v1alpha1",
        "kind": "PostgresInstance",
        "metadata": {"name": name, "generation": 1},
        "spec": {
            "host": endpoint.host,
            "port": endpoint.port,
            "maintenanceDatabase": endpoint.maintenance_database,
            "sslMode": endpoint.ssl_mode,
            "credentialsSecretRef": {"name": f"{name}-superuser", "namespace": OPERATOR_NS},
        },
    }


def _database(name: str, instance: str, database: str, **spec: Any) -> dict[str, Any]:
    return {
        "apiVersion": "postgres.ourcommunity.com.au/v1alpha1",
        "kind": "PostgresDB",
        "metadata": {"name": name, "namespace": NS, "generation": 1, "uid": f"{name}-uid"},
        "spec": {
            "instanceRef": {"name": instance},
            "databaseName": database,
            "schemas": [{"name": "public"}, {"name": "audit"}],
            **spec,
        },
    }


@pytest.fixture
async def world(
    pools: PoolRegistry,
    endpoint: Endpoint,
    target_endpoint: Endpoint,
    source_from_target: dict[str, str],
    target_from_source: dict[str, str],
    unique: str,
) -> AsyncIterator[World]:
    for side in (endpoint, target_endpoint):
        async with pools.connection(side) as conn:
            server = await describe_server(conn)
        if server.major_version < 16:
            pytest.skip(f"replication needs PostgreSQL 16+, {side.instance} is {server.version}")
    k8s = FakeK8sClient()
    for name, side in (("src", endpoint), ("dst", target_endpoint)):
        k8s.core.seed(
            OPERATOR_NS, f"{name}-superuser", {"username": side.username, "password": side.password}
        )
        k8s.custom.add(_instance(name, side), PLURAL_INSTANCE)
    source_db, target_db = f"rs_src_{unique}", f"rs_dst_{unique}"
    databases = [
        _database("orders", "src", source_db, retentionPolicy="DROP"),
        _database("orders-copy", "dst", target_db, retentionPolicy="DROP"),
    ]
    for obj in databases:
        k8s.custom.add(obj, PLURAL_DATABASE)

    ctx = ReconcileContext(k8s, settings=Settings(operator_namespace=OPERATOR_NS))  # type: ignore[arg-type]
    for obj in databases:
        await reconcile_database(ctx, obj)
    replication = {
        "apiVersion": "postgres.ourcommunity.com.au/v1alpha1",
        "kind": "PostgresReplication",
        "metadata": {"name": f"rs-{unique}", "namespace": NS, "generation": 1},
        "spec": {
            # The target server reaches the source by its container name, not
            # by the host port the operator uses.
            "source": {
                "dbRef": {"name": "orders"},
                "connection": {
                    "host": source_from_target["host"],
                    "port": int(source_from_target.get("port", "5432")),
                    "sslMode": "disable",
                },
            },
            "target": {"dbRef": {"name": "orders-copy"}},
            "schemas": [{"name": "public"}, {"name": "audit"}],
            "tables": [
                {
                    "schema": "public",
                    "name": "orders",
                    "columns": ["id", "region"],
                    "rowFilter": {"all": [{"column": "id", "op": "Gt", "value": 0}]},
                }
            ],
        },
    }
    k8s.custom.add(replication, PLURAL_REPLICATION)
    world = World(
        ctx, k8s, pools, endpoint, target_endpoint, source_db, target_db, replication,
        unique=unique, target_from_source=target_from_source,
    )
    await world.on_source(*SOURCE_TABLES, as_owner=True)
    await world.on_target(*TARGET_TABLES, as_owner=True)
    try:
        yield world
    finally:
        for each in [world, *world.extras]:
            if each.replication.get("status"):
                with contextlib.suppress(Exception):
                    await teardown_replication(ctx, each.replication)
        await ctx.close()
        for each in [world, *world.extras]:
            login = (each.replication.get("status") or {}).get("source", {}).get("login")
            await _force_clean(pools, each.target, each.target_db, source_is_target_server=each.reversed)
            await drop_database_hard(pools, each.source, each.source_db)
            await drop_database_hard(pools, each.target, each.target_db)
            await drop_roles_hard(pools, each.source, list(group_role_names(each.source_db).values()))
            await drop_roles_hard(pools, each.target, list(group_role_names(each.target_db).values()))
            if login:
                superuser_execute(f'DROP ROLE IF EXISTS "{login}"', target=each.reversed)


async def _force_clean(
    pools: PoolRegistry, target: Endpoint, target_db: str, *, source_is_target_server: bool = False
) -> None:
    """Whatever a failed test left: a subscription blocks DROP DATABASE, a slot pins WAL."""
    async with pools.connection(target) as conn:
        exists = await fetch_scalar(
            conn, sql.SQL("SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname = %s)"), (target_db,)
        )
    if exists:
        async with pools.connection(target, target_db) as conn:
            names = await fetch_column(
                conn,
                sql.SQL(
                    "SELECT subname FROM pg_subscription WHERE subdbid = "
                    "(SELECT oid FROM pg_database WHERE datname = current_database())"
                ),
            )
            for name in names:
                for statement in (
                    "ALTER SUBSCRIPTION {} DISABLE",
                    "ALTER SUBSCRIPTION {} SET (slot_name = NONE)",
                    "DROP SUBSCRIPTION {}",
                ):
                    await execute(conn, sql.SQL(statement).format(sql.Identifier(name)))
    leftover = r"slot_name LIKE 'pgop\_%' OR slot_name ~ '^pg_[0-9]+_sync_'"
    on_source = source_is_target_server
    superuser_execute(
        f"SELECT pg_terminate_backend(active_pid) FROM pg_replication_slots "  # noqa: S608 - test cleanup, constant SQL
        f"WHERE active_pid IS NOT NULL AND ({leftover})",
        target=on_source,
    )
    for _ in range(20):
        if not superuser_execute(
            f"SELECT 1 FROM pg_replication_slots WHERE active AND ({leftover})",  # noqa: S608
            target=on_source,
        ):
            break
        await asyncio.sleep(0.25)
    superuser_execute(
        f"SELECT pg_drop_replication_slot(slot_name) FROM pg_replication_slots WHERE {leftover}",  # noqa: S608
        target=on_source,
    )

