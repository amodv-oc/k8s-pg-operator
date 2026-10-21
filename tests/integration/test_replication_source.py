"""The source side of a PostgresReplication, against two real servers.

Drives the real reconcilers - PostgresDB for both databases, then
PostgresReplication - as the RDS-like managing role, with tables created as the
owner group exactly as setRoleForOwners makes migrations create them. Proves
what reaches the publication, what is held back and why, and that a converged
pass changes nothing.

No subscription exists yet, so nothing streams and no superuser emulation is
needed: every statement here runs as pgadmin.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import psycopg
import pytest
from psycopg import sql

from pg_operator.config import Settings
from pg_operator.constants import (
    PLURAL_DATABASE,
    PLURAL_INSTANCE,
    PLURAL_REPLICATION,
)
from pg_operator.errors import ConfigurationError
from pg_operator.naming import group_role_names
from pg_operator.postgres.connection import Endpoint, PoolRegistry
from pg_operator.postgres.server import describe_server
from pg_operator.postgres.sql import execute, fetch_all, fetch_scalar
from pg_operator.reconcile.context import ReconcileContext
from pg_operator.reconcile.database import reconcile_database
from pg_operator.reconcile.replication import (
    build_status,
    ownership_marker,
    reconcile_replication,
    teardown_replication,
)

from ..conftest import (
    drop_database_hard,
    drop_roles_hard,
    requires_replication_harness,
    superuser_execute,
)
from ..fakes import FakeK8sClient

pytestmark = [requires_replication_harness, pytest.mark.replication, pytest.mark.asyncio]

NS = "team-a"
OPERATOR_NS = "pg-operator"


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
    last_changes: list[str] = field(default_factory=list)

    @property
    def groups(self) -> dict[str, str]:
        return group_role_names(self.source_db)

    async def run(self) -> dict[str, Any]:
        """One reconcile pass, with status written back as the handler does."""
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

    async def publication(self) -> dict[str, tuple[list[str] | None, str | None]]:
        """``schema.table -> (columns, row filter)`` as the server holds them."""
        name = self.replication["status"]["source"]["publication"]
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

    def excluded(self) -> dict[str, str]:
        return {e["table"]: e["reason"] for e in self.replication["status"]["excludedTables"]}


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


def _database(name: str, instance: str, database: str) -> dict[str, Any]:
    return {
        "apiVersion": "postgres.ourcommunity.com.au/v1alpha1",
        "kind": "PostgresDB",
        "metadata": {"name": name, "namespace": NS, "generation": 1, "uid": f"{name}-uid"},
        "spec": {
            "instanceRef": {"name": instance},
            "databaseName": database,
            "schemas": [{"name": "public"}, {"name": "audit"}],
        },
    }


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


@pytest.fixture
async def world(
    pools: PoolRegistry, endpoint: Endpoint, target_endpoint: Endpoint, unique: str
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
    databases = [_database("orders", "src", source_db), _database("orders-copy", "dst", target_db)]
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
            "source": {"dbRef": {"name": "orders"}},
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
    world = World(ctx, k8s, pools, endpoint, target_endpoint, source_db, target_db, replication)
    await world.on_source(*SOURCE_TABLES, as_owner=True)
    await world.on_target(*TARGET_TABLES, as_owner=True)
    try:
        yield world
    finally:
        login = (replication.get("status") or {}).get("source", {}).get("login")
        if replication.get("status"):
            await teardown_replication(ctx, replication)
        # The reconciler's own pools count as open connections and block the drops.
        await ctx.close()
        await drop_database_hard(pools, endpoint, source_db)
        await drop_database_hard(pools, target_endpoint, target_db)
        await drop_roles_hard(pools, endpoint, list(group_role_names(source_db).values()))
        await drop_roles_hard(pools, target_endpoint, list(group_role_names(target_db).values()))
        if login:
            superuser_execute(f'DROP ROLE IF EXISTS "{login}"')


# ---------------------------------------------------------------------------


async def test_the_publication_carries_what_both_servers_can_take(world: World) -> None:
    status = await world.run()

    assert status["phase"] == "Pending"
    assert status["publishedTables"] == ["audit.events", "public.orders"]
    assert world.excluded() == {
        "public.nokey": "NoReplicaIdentity",
        "public.only_source": "TargetTableMissing",
    }
    published = await world.publication()
    columns, row_filter = published["public.orders"]
    assert columns == ["id", "region"]
    assert row_filter is not None and "id > 0" in row_filter
    assert published["audit.events"] == (None, None)
    assert status["rowFilters"] == [
        {"table": "public.orders", "specHash": status["rowFilters"][0]["specHash"], "applied": row_filter}
    ]
    conditions = {c["type"]: c for c in status["conditions"]}
    assert conditions["Drifted"]["reason"] == "MultipleIssues"
    assert conditions["Synced"]["status"] == "False"


async def test_a_held_back_table_keeps_the_sources_writes_working(world: World) -> None:
    """The point of NoReplicaIdentity: published, this UPDATE would be refused."""
    await world.run()
    await world.on_source(
        "INSERT INTO public.nokey VALUES ('a')", "UPDATE public.nokey SET v = 'b'", as_owner=True
    )


async def test_the_login_streams_through_rds_replication_and_reads_through_ro(world: World) -> None:
    status = await world.run()
    login = status["source"]["login"]
    assert status["created"] == {"login": True, "secret": True, "publication": True}
    assert set(status["grantedMemberships"]) == {"rds_replication", world.groups["readOnly"]}

    async with world.pools.connection(world.source) as conn:
        marker = await fetch_scalar(
            conn,
            sql.SQL("SELECT shobj_description(oid, 'pg_authid') FROM pg_roles WHERE rolname = %s"),
            (login,),
        )
        assert marker == ownership_marker(NS, world.replication["metadata"]["name"])
        # Membership only: the attribute is RDS's to confer, never the operator's.
        assert await fetch_scalar(
            conn, sql.SQL("SELECT rolreplication FROM pg_roles WHERE rolname = %s"), (login,)
        ) is False

    credentials = status["credentialsSecret"]
    assert credentials["namespace"] == OPERATOR_NS and credentials["generated"] is True
    stored = world.k8s.core.get(OPERATOR_NS, credentials["name"])
    assert stored is not None and stored["username"] == login

    # The initial copy will read as this login: prove it can.
    await world.on_source("INSERT INTO public.orders VALUES (1, 'AU', 10)", as_owner=True)
    as_login = Endpoint(
        instance="rs-login",
        host=world.source.host,
        port=world.source.port,
        username=login,
        password=stored["password"],
        maintenance_database=world.source_db,
        ssl_mode=world.source.ssl_mode,
    )
    async with world.pools.connection(as_login) as conn:
        assert await fetch_scalar(conn, sql.SQL("SELECT count(*) FROM public.orders")) == 1
    await world.pools.close_instance(as_login.instance)


async def test_a_converged_pass_changes_nothing(world: World) -> None:
    first = await world.run()
    before = await world.publication()
    second = await world.run()
    assert world.last_changes == []
    assert await world.publication() == before
    for key in ("publishedTables", "excludedTables", "rowFilters", "created", "grantedMemberships"):
        assert second[key] == first[key], key


async def test_a_table_created_on_the_target_later_is_published(world: World) -> None:
    await world.run()
    assert world.excluded()["public.only_source"] == "TargetTableMissing"
    await world.on_target("CREATE TABLE public.only_source (id int PRIMARY KEY)", as_owner=True)
    status = await world.run()
    assert "public.only_source" in status["publishedTables"]
    assert "public.only_source" not in world.excluded()


async def test_losing_the_replica_identity_unpublishes_the_table(world: World) -> None:
    """Design D6: the source's writes must work again on the very next pass."""
    await world.run()
    assert "audit.events" in await world.publication()
    await world.on_source("ALTER TABLE audit.events DROP CONSTRAINT events_pkey", as_owner=True)

    status = await world.run()
    assert "audit.events" not in await world.publication()
    assert world.excluded()["audit.events"] == "NoReplicaIdentity"
    assert "audit.events" not in status["publishedTables"]
    await world.on_source(
        "INSERT INTO audit.events VALUES (1, 'x')", "UPDATE audit.events SET kind = 'y'", as_owner=True
    )


async def test_a_changed_filter_is_reapplied_and_an_edited_one_restored(world: World) -> None:
    await world.run()
    world.replication["spec"]["tables"][0]["rowFilter"] = {"all": [{"column": "id", "op": "Gt", "value": 10}]}
    await world.run()
    assert "id > 10" in (await world.publication())["public.orders"][1]

    # Someone edits it out of band; the next pass puts the spec's back.
    name = world.replication["status"]["source"]["publication"]
    await world.on_source(
        f"ALTER PUBLICATION {name} SET TABLE public.orders (id, region) WHERE (id > 99), audit.events"
    )
    await world.run()
    assert "id > 10" in (await world.publication())["public.orders"][1]
    await world.run()
    assert world.last_changes == []


async def test_an_operand_the_server_cannot_convert_holds_back_only_its_table(world: World) -> None:
    world.replication["spec"]["tables"][0]["rowFilter"] = {
        "all": [{"column": "id", "op": "Eq", "value": "not-a-number"}]
    }
    status = await world.run()
    assert world.excluded()["public.orders"] == "InvalidRowFilter"
    assert status["publishedTables"] == ["audit.events"]


async def test_a_deleted_credentials_secret_is_regenerated_and_applied(world: World) -> None:
    status = await world.run()
    secret = status["credentialsSecret"]["name"]
    old = world.k8s.core.get(OPERATOR_NS, secret)["password"]
    await world.k8s.core.delete_namespaced_secret(name=secret, namespace=OPERATOR_NS)

    await world.run()
    new = world.k8s.core.get(OPERATOR_NS, secret)["password"]
    assert new != old
    assert any("generated and applied a new password" in change for change in world.last_changes)
    login = status["source"]["login"]
    dsn = psycopg.conninfo.make_conninfo(
        host=world.source.host, port=world.source.port, dbname=world.source_db,
        user=login, password=new, sslmode=world.source.ssl_mode,
    )
    async with await psycopg.AsyncConnection.connect(dsn) as conn:
        assert (await (await conn.execute("SELECT current_user")).fetchone()) == (login,)


async def test_a_provided_login_is_used_and_left_in_place(world: World, unique: str) -> None:
    login, password = f"rs_byo_{unique}", f"byo-{unique}-password"
    # Provisioned the way a DBA would: by someone who can grant REPLICATION.
    superuser_execute(f"CREATE ROLE \"{login}\" LOGIN REPLICATION PASSWORD '{password}'")
    try:
        world.k8s.core.seed(NS, "byo-login", {"username": login, "password": password})
        world.replication["spec"]["source"]["credentialsSecretRef"] = {"name": "byo-login"}
        status = await world.run()
        assert status["source"]["login"] == login
        assert status["credentialsSecret"] == {"namespace": NS, "name": "byo-login", "generated": False}
        assert status["grantedMemberships"] == [world.groups["readOnly"]]
        assert "login" not in status["created"] and "secret" not in status["created"]

        message = await teardown_replication(world.ctx, world.replication)
        assert f"{world.groups['readOnly']} membership of {login}" in message
        world.replication.pop("status")
        async with world.pools.connection(world.source) as conn:
            assert await fetch_scalar(
                conn, sql.SQL("SELECT count(*) FROM pg_roles WHERE rolname = %s"), (login,)
            ) == 1
            assert not await fetch_scalar(
                conn, sql.SQL("SELECT pg_has_role(%s, %s, 'MEMBER')"), (login, world.groups["readOnly"])
            )
    finally:
        superuser_execute(f'DROP ROLE IF EXISTS "{login}"')


async def test_teardown_removes_what_it_created_and_keeps_the_target(world: World) -> None:
    status = await world.run()
    login = status["source"]["login"]
    publication = status["source"]["publication"]
    await world.on_target("INSERT INTO public.orders VALUES (7, 'AU', 1)", as_owner=True)

    message = await teardown_replication(world.ctx, world.replication)
    world.replication.pop("status")
    assert publication in message and login in message

    async with world.pools.connection(world.source, world.source_db) as conn:
        assert not await fetch_scalar(
            conn, sql.SQL("SELECT EXISTS (SELECT 1 FROM pg_publication WHERE pubname = %s)"), (publication,)
        )
        assert not await fetch_scalar(
            conn, sql.SQL("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = %s)"), (login,)
        )
    assert world.k8s.core.get(OPERATOR_NS, status["credentialsSecret"]["name"]) is None
    async with world.pools.connection(world.target, world.target_db) as conn:
        assert await fetch_scalar(conn, sql.SQL("SELECT count(*) FROM public.orders")) == 1


async def test_objects_from_a_failed_pass_are_still_torn_down(world: World) -> None:
    """A pass that fails after creating objects must not hide them from teardown.

    A failed pass publishes only its conditions, so each object is recorded the
    moment it exists. Here the target database vanishes mid-pass: the publication,
    login and Secret already exist on the source when the pass fails.
    """
    await world.ctx.pools.close_database("dst", world.target_db)
    await drop_database_hard(world.pools, world.target, world.target_db)
    with pytest.raises(Exception):  # noqa: B017 - any failure will do; what it leaves is the point
        await world.run()

    recorded = world.replication["status"]
    assert recorded["created"] == {"publication": True, "login": True, "secret": True}
    message = await teardown_replication(world.ctx, world.replication)
    world.replication.pop("status")
    assert recorded["source"]["publication"] in message
    assert recorded["source"]["login"] in message
    assert world.k8s.core.get(OPERATOR_NS, recorded["credentialsSecret"]["name"]) is None


async def test_a_pass_that_can_never_succeed_creates_nothing(world: World) -> None:
    """Name conflicts are found before anything is created, not after."""
    from pg_operator.naming import replication_names

    taken = replication_names(NS, world.replication["metadata"]["name"]).publication
    await world.on_source(f"CREATE PUBLICATION {taken}")
    try:
        with pytest.raises(ConfigurationError, match="will not take it over"):
            await world.run()
        assert "status" not in world.replication
        login = replication_names(NS, world.replication["metadata"]["name"]).login
        async with world.pools.connection(world.source) as conn:
            assert not await fetch_scalar(
                conn, sql.SQL("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = %s)"), (login,)
            )
    finally:
        await world.on_source(f"DROP PUBLICATION {taken}")


async def test_an_existing_publication_is_not_taken_over(world: World) -> None:
    from pg_operator.naming import replication_names

    taken = replication_names(NS, world.replication["metadata"]["name"]).publication
    await world.on_source(f"CREATE PUBLICATION {taken}")
    with pytest.raises(ConfigurationError, match="will not take it over"):
        await world.run()
    await world.on_source(f"DROP PUBLICATION {taken}")


async def test_a_partitioned_table_publishes_its_root(world: World) -> None:
    await world.on_source(
        "CREATE TABLE public.readings (id int, at date, PRIMARY KEY (id, at)) PARTITION BY RANGE (at)",
        "CREATE TABLE public.readings_2026 PARTITION OF public.readings "
        "FOR VALUES FROM ('2026-01-01') TO ('2027-01-01')",
        as_owner=True,
    )
    await world.on_target(
        "CREATE TABLE public.readings (id int, at date, PRIMARY KEY (id, at)) PARTITION BY RANGE (at)",
        as_owner=True,
    )
    await world.run()
    # Without publishViaPartitionRoot, changes arrive under the leaf's name.
    assert world.excluded()["public.readings"] == "TargetTableMissing"
    await world.on_target(
        "CREATE TABLE public.readings_2026 PARTITION OF public.readings "
        "FOR VALUES FROM ('2026-01-01') TO ('2027-01-01')",
        as_owner=True,
    )
    status = await world.run()
    assert "public.readings" in status["publishedTables"]
    assert "public.readings_2026" not in status["publishedTables"]

