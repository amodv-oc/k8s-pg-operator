"""The source catalog a replication judges its tables on, read from a live server.

``describe_source_tables`` runs on every pass of every replication, and
eligibility leans on the details of what it returns: which relations are
top-level entries, which leaves hang off each partitioned table, and the
per-leaf facts. Its output is pinned here on a deliberately awkward catalog.
Nothing here needs a second server or a superuser, so it runs on every
version the single-server suite covers.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import psycopg
import pytest
from psycopg import sql

from pg_operator.postgres import publication as pub
from pg_operator.postgres import roles as pgroles
from pg_operator.postgres.connection import Endpoint, PoolRegistry
from pg_operator.postgres.server import describe_server
from pg_operator.postgres.sql import execute, fetch_all

from ..conftest import drop_database_hard, drop_roles_hard, requires_postgres

pytestmark = [requires_postgres, pytest.mark.asyncio]


@pytest.fixture
async def scratch(
    pools: PoolRegistry, endpoint: Endpoint, unique: str
) -> AsyncIterator[tuple[str, list[str]]]:
    """An empty database owned by the managing role, and a list of roles to drop after."""
    name = f"pgop_{unique}"
    roles: list[str] = []
    async with pools.connection(endpoint) as conn:
        await execute(conn, sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        yield name, roles
    finally:
        await drop_database_hard(pools, endpoint, name)
        await drop_roles_hard(pools, endpoint, roles)


async def _run(conn: psycopg.AsyncConnection, *statements: str) -> None:
    for statement in statements:
        await execute(conn, sql.SQL(statement))  # type: ignore[arg-type]


def _snapshot(table: pub.SourceTable) -> dict[str, Any]:
    """Everything but the OID, which differs on every run; columns in order."""
    return {
        "kind": table.kind,
        "persistent": table.persistent,
        "is_partition": table.is_partition,
        "owner": table.owner,
        "owner_usable": table.owner_usable,
        "row_security": table.row_security,
        "replica_identity": table.replica_identity,
        "identity_columns": (
            sorted(table.identity_columns) if table.identity_columns is not None else None
        ),
        "columns": list(table.columns.items()),
        "generated": sorted(table.generated),
        "readable_by_login": table.readable_by_login,
        # Which leaves, not their order: that is pinned by the next test.
        "leaves": sorted(leaf.qualified for leaf in table.leaves),
    }


async def test_the_source_catalog_is_described_exactly(
    pools: PoolRegistry, endpoint: Endpoint, scratch: tuple[str, list[str]]
) -> None:
    database, roles = scratch
    login, stranger, via = (f"{database}_login", f"{database}_stranger", f"{database}_via")
    async with pools.connection(endpoint) as conn:
        server = await describe_server(conn)
        admin = server.current_user
        for role in (login, stranger):
            await pgroles.ensure_group_role(conn, role)
            roles.append(role)
        # A role the managing role can SET ROLE to but holds no privileges of,
        # on every version: membership through a NOINHERIT intermediary.
        await execute(conn, sql.SQL("CREATE ROLE {} NOLOGIN NOINHERIT").format(sql.Identifier(via)))
        roles.append(via)
        await pgroles.grant_membership(conn, stranger, via)
        await pgroles.ensure_self_membership(
            conn, via, per_grant_options=server.supports_per_grant_options
        )

    async with pools.connection(endpoint, database) as conn:
        await _run(
            conn,
            "CREATE SCHEMA app",
            "CREATE SCHEMA other",
            "CREATE SCHEMA extra",
            # Plain tables, one per kind of replica identity.
            "CREATE TABLE app.plain (id int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,"
            " note text NOT NULL DEFAULT '', doubled int GENERATED ALWAYS AS (id * 2) STORED)",
            "CREATE TABLE app.covered (id int, region text, PRIMARY KEY (id) INCLUDE (region))",
            "CREATE TABLE app.keyless (v text)",
            "CREATE TABLE app.full_identity (id int, v varchar(12))",
            "ALTER TABLE app.full_identity REPLICA IDENTITY FULL",
            "CREATE TABLE app.by_index (id int NOT NULL, code text NOT NULL, v text)",
            "CREATE UNIQUE INDEX by_index_code ON app.by_index (code)",
            "ALTER TABLE app.by_index REPLICA IDENTITY USING INDEX by_index_code",
            "CREATE TABLE app.deferred (id int PRIMARY KEY DEFERRABLE)",
            "CREATE UNLOGGED TABLE app.scratch (id int PRIMARY KEY)",
            "CREATE VIEW app.recent AS SELECT id FROM app.plain",
            "CREATE SEQUENCE app.counter",
            # Two levels of partitioning: a leaf in another schema, an
            # intermediate with no partitions, an unlogged leaf, a default one.
            "CREATE TABLE app.events (id int, at date, kind text, PRIMARY KEY (id, at))"
            " PARTITION BY RANGE (at)",
            "CREATE TABLE app.events_2025 PARTITION OF app.events"
            " FOR VALUES FROM ('2025-01-01') TO ('2026-01-01')",
            "CREATE TABLE app.events_2026 PARTITION OF app.events"
            " FOR VALUES FROM ('2026-01-01') TO ('2027-01-01') PARTITION BY HASH (id)",
            "CREATE TABLE app.events_2026_h0 PARTITION OF app.events_2026"
            " FOR VALUES WITH (MODULUS 2, REMAINDER 0)",
            "CREATE TABLE other.events_2026_h1 PARTITION OF app.events_2026"
            " FOR VALUES WITH (MODULUS 2, REMAINDER 1)",
            "CREATE TABLE app.events_2027 PARTITION OF app.events"
            " FOR VALUES FROM ('2027-01-01') TO ('2028-01-01') PARTITION BY HASH (id)",
            "CREATE UNLOGGED TABLE app.events_scratch PARTITION OF app.events"
            " FOR VALUES FROM ('2000-01-01') TO ('2001-01-01')",
            "CREATE TABLE other.events_old PARTITION OF app.events DEFAULT",
            "ALTER TABLE app.events_2025 REPLICA IDENTITY FULL",
            "ALTER TABLE app.events_2026_h0 ENABLE ROW LEVEL SECURITY",
            # A partitioned table with no partitions at all.
            "CREATE TABLE app.empty (id int PRIMARY KEY) PARTITION BY RANGE (id)",
            # One whose second partition will be left half-detached, below.
            "CREATE TABLE app.ledger (id int PRIMARY KEY) PARTITION BY RANGE (id)",
            "CREATE TABLE app.ledger_1 PARTITION OF app.ledger FOR VALUES FROM (0) TO (10)",
            "CREATE TABLE app.ledger_2 PARTITION OF app.ledger FOR VALUES FROM (10) TO (20)",
            # Outside the selected schema: two named, one not.
            "CREATE TABLE extra.named (id int PRIMARY KEY, v text)",
            "CREATE TABLE extra.parted (id int PRIMARY KEY) PARTITION BY RANGE (id)",
            "CREATE TABLE extra.parted_1 PARTITION OF extra.parted FOR VALUES FROM (0) TO (10)",
            "CREATE TABLE extra.unnamed (id int)",
        )
        await execute(
            conn,
            sql.SQL("GRANT SELECT ON {} TO {}").format(
                sql.SQL(", ").join(
                    sql.Identifier(*name.split("."))
                    for name in (
                        "app.plain", "app.covered", "app.events", "app.events_2025",
                        "app.events_2026_h0", "other.events_old", "extra.parted",
                    )
                ),
                sql.Identifier(login),
            ),
        )
        await execute(
            conn,
            sql.SQL("GRANT CREATE ON SCHEMA other TO {}").format(sql.Identifier(stranger)),
        )
        await execute(
            conn,
            sql.SQL("ALTER TABLE other.events_2026_h1 OWNER TO {}").format(
                sql.Identifier(stranger)
            ),
        )
        # DETACH ... CONCURRENTLY marks the partition "detach pending" and
        # commits, then waits out every transaction using its table. Cancelled
        # in that wait, it leaves the partition attached but pending.
        async with pools.connection(endpoint, database) as holder, holder.transaction():
            await fetch_all(holder, sql.SQL("SELECT count(*) FROM app.ledger"))
            async with await psycopg.AsyncConnection.connect(
                endpoint.conninfo(database, statement_timeout_ms=500, app_name="pgop-test"),
                autocommit=True,
            ) as detacher:
                with pytest.raises(psycopg.errors.QueryCanceled):
                    await detacher.execute(
                        "ALTER TABLE app.ledger DETACH PARTITION app.ledger_2 CONCURRENTLY"
                    )
        assert await fetch_all(
            conn,
            sql.SQL("SELECT inhdetachpending FROM pg_inherits WHERE inhrelid = 'app.ledger_2'::regclass"),
        ) == [(True,)]

        described = await pub.describe_source_tables(
            conn,
            schemas=["app"],
            tables=[
                ("extra", "named"),
                ("extra", "parted"),
                ("other", "events_old"),
                ("app", "plain"),
                ("extra", "missing"),
            ],
            login=login,
        )
        oids = dict(
            await fetch_all(
                conn,
                sql.SQL(
                    "SELECT n.nspname || '.' || c.relname, c.oid FROM pg_class c"
                    " JOIN pg_namespace n ON n.oid = c.relnamespace"
                    " WHERE n.nspname IN ('app', 'other', 'extra')"
                ),
            )
        )
        # The leaves are exactly what the server's own partition-tree function
        # calls leaves.
        for table in described.values():
            if table.kind == "p":
                tree = await fetch_all(
                    conn,
                    sql.SQL(
                        "SELECT relid::oid FROM pg_partition_tree(%s::oid::regclass)"
                        " WHERE isleaf AND relid <> %s::oid::regclass"
                    ),
                    (table.oid, table.oid),
                )
                assert sorted(leaf.oid for leaf in table.leaves) == sorted(
                    int(row[0]) for row in tree
                ), table.qualified

    every = [*described.values(), *(leaf for table in described.values() for leaf in table.leaves)]
    assert all(table.oid == oids[table.qualified] for table in every)
    # A leaf is described the same whether it is reached on its own or through
    # its partitioned table.
    for table in described.values():
        for leaf in table.leaves:
            if leaf.qualified in described:
                assert leaf == described[leaf.qualified]
            assert leaf.leaves == ()

    def expected(*layers: dict[str, Any]) -> dict[str, Any]:
        base: dict[str, Any] = {
            "kind": "r", "persistent": True, "is_partition": False, "owner": admin,
            "owner_usable": True, "row_security": False, "replica_identity": "d",
            "identity_columns": None, "columns": [], "generated": [],
            "readable_by_login": False, "leaves": [],
        }
        for layer in layers:
            base |= layer
        return base

    integer = [("id", "integer")]
    keyed = {"identity_columns": ["id"], "columns": integer}
    readable = {"readable_by_login": True}
    partitioned = {"kind": "p"}
    event = {"is_partition": True, "identity_columns": ["at", "id"],
             "columns": [("id", "integer"), ("at", "date"), ("kind", "text")]}
    leaf = {
        "app.events_2025": expected(
            event, readable, {"replica_identity": "f", "identity_columns": ["at", "id", "kind"]}
        ),
        "app.events_2026_h0": expected(event, readable, {"row_security": True}),
        "app.events_scratch": expected(event, {"persistent": False}),
        "other.events_2026_h1": expected(event, {"owner": stranger, "owner_usable": False}),
        "other.events_old": expected(event, readable),
        "extra.parted_1": expected(keyed, {"is_partition": True}),
        "app.ledger_1": expected(keyed, {"is_partition": True}),
    }
    assert {key: _snapshot(table) for key, table in described.items()} == {
        "app.plain": expected(
            keyed, readable,
            {"columns": [("id", "integer"), ("note", "text")], "generated": ["doubled"]},
        ),
        "app.covered": expected(
            keyed, readable, {"columns": [("id", "integer"), ("region", "text")]}
        ),
        "app.keyless": expected({"columns": [("v", "text")]}),
        "app.full_identity": expected(
            {
                "replica_identity": "f",
                "identity_columns": ["id", "v"],
                "columns": [("id", "integer"), ("v", "character varying(12)")],
            }
        ),
        "app.by_index": expected(
            {
                "replica_identity": "i",
                "identity_columns": ["code"],
                "columns": [("id", "integer"), ("code", "text"), ("v", "text")],
            }
        ),
        "app.deferred": expected({"columns": integer}),
        "app.scratch": expected(keyed, {"persistent": False}),
        "app.recent": expected({"kind": "v", "replica_identity": "n", "columns": integer}),
        "app.events": expected(
            event, partitioned, readable,
            {
                "is_partition": False,
                # Every leaf at every level, wherever it lives; never the
                # partitioned tables in between.
                "leaves": [
                    "app.events_2025", "app.events_2026_h0", "app.events_scratch",
                    "other.events_2026_h1", "other.events_old",
                ],
            },
        ),
        "app.events_2026": expected(
            event, partitioned, {"leaves": ["app.events_2026_h0", "other.events_2026_h1"]}
        ),
        "app.events_2027": expected(event, partitioned),
        "app.events_2025": leaf["app.events_2025"],
        "app.events_2026_h0": leaf["app.events_2026_h0"],
        "app.events_scratch": leaf["app.events_scratch"],
        "app.empty": expected(keyed, partitioned),
        # Still a partition until the detach is finished, but no longer a leaf.
        "app.ledger": expected(keyed, partitioned, {"leaves": ["app.ledger_1"]}),
        "app.ledger_1": leaf["app.ledger_1"],
        "app.ledger_2": expected(keyed, {"is_partition": True}),
        "extra.named": expected(keyed, {"columns": [("id", "integer"), ("v", "text")]}),
        "extra.parted": expected(keyed, partitioned, readable, {"leaves": ["extra.parted_1"]}),
        "other.events_old": leaf["other.events_old"],
    }
    assert {
        each.qualified: _snapshot(each) for table in described.values() for each in table.leaves
    } == leaf


async def test_the_statement_count_does_not_grow_with_partitioned_tables(
    pools: PoolRegistry,
    endpoint: Endpoint,
    scratch: tuple[str, list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every pass reads the source catalog, so its cost must not scale per table.

    It once took four round trips for each partitioned table, one after the
    other, on every pass of every replication.
    """
    database, _ = scratch
    ddl = ["CREATE SCHEMA narrow", "CREATE SCHEMA wide"]
    for schema, count in (("narrow", 1), ("wide", 12)):
        for i in range(count):
            ddl.append(
                f"CREATE TABLE {schema}.p{i} (id int PRIMARY KEY, v text) PARTITION BY RANGE (id)"
            )
            # Created out of name order, so an order that merely follows
            # creation would show.
            for j in (2, 0, 1):
                ddl.append(
                    f"CREATE TABLE {schema}.p{i}_{j} PARTITION OF {schema}.p{i}"
                    f" FOR VALUES FROM ({j * 100}) TO ({(j + 1) * 100})"
                )

    statements = 0
    real_execute = psycopg.AsyncCursor.execute

    async def counting(self: psycopg.AsyncCursor[Any], *args: Any, **kwargs: Any) -> Any:
        nonlocal statements
        statements += 1
        return await real_execute(self, *args, **kwargs)

    async with pools.connection(endpoint, database) as conn:
        await _run(conn, *ddl)
        counted: dict[str, int] = {}
        described: dict[str, pub.SourceTable] = {}
        for schema in ("narrow", "wide"):
            statements = 0
            with monkeypatch.context() as patched:
                patched.setattr(psycopg.AsyncCursor, "execute", counting)
                described |= await pub.describe_source_tables(
                    conn, schemas=[schema], tables=[], login=endpoint.username
                )
            counted[schema] = statements

    assert counted["wide"] == counted["narrow"], counted
    parents = [table for table in described.values() if table.kind == "p"]
    assert len(parents) == 13
    for parent in parents:
        # In name order, so a status message that lists them is stable.
        assert [leaf.qualified for leaf in parent.leaves] == [
            f"{parent.qualified}_{j}" for j in range(3)
        ]
