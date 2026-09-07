"""End-to-end checks of the OWNER / RW / RO model against a live server.

These run as a non-superuser CREATEDB/CREATEROLE member of ``rds_superuser``,
the same constraint the operator has on RDS. Anything that silently depends on
real superuser rights fails here.
"""

from __future__ import annotations

import pytest
from psycopg import sql

from pg_operator.postgres import database as db
from pg_operator.postgres import roles as pgroles
from pg_operator.postgres.connection import Endpoint, PoolRegistry
from pg_operator.postgres.privileges import (
    GrantOptions,
    GroupRoles,
    apply_default_privileges,
    apply_schema_privileges,
    observed_schema_grants,
)
from pg_operator.postgres.server import describe_server
from pg_operator.postgres.sql import execute, fetch_scalar

from ..conftest import drop_database_hard, drop_roles_hard, requires_postgres

pytestmark = [requires_postgres, pytest.mark.asyncio]


async def _provision(conn, name: str, roles: GroupRoles, server) -> None:
    """Do what the database reconciler's phase 1 does."""
    for role in roles.all:
        await pgroles.ensure_group_role(conn, role)
    await pgroles.ensure_self_membership(
        conn, roles.owner, per_grant_options=server.supports_per_grant_options
    )
    await db.create_database(conn, name, owner=roles.owner)
    await db.revoke_public_database_privileges(conn, name)
    await db.grant_database_connect(
        conn, name, [roles.owner, roles.read_write], with_temp=True
    )
    await db.grant_database_connect(conn, name, [roles.read_only], with_temp=False)


async def _teardown(pools: PoolRegistry, endpoint: Endpoint, name: str, roles: GroupRoles):
    await drop_database_hard(pools, endpoint, name)
    await drop_roles_hard(pools, endpoint, roles.all)


@pytest.fixture
async def provisioned(pools: PoolRegistry, endpoint: Endpoint, unique: str):
    """A database with its three group roles and the `public` schema granted."""
    name = f"pgop_{unique}"
    roles = GroupRoles(
        owner=f"{name}_owner", read_write=f"{name}_rw", read_only=f"{name}_ro"
    )
    async with pools.connection(endpoint) as conn:
        server = await describe_server(conn)
        await _provision(conn, name, roles, server)

    options = GrantOptions(
        public_schema_locked_by_default=server.public_schema_is_locked_down
    )
    async with pools.connection(endpoint, name) as conn:
        await db.ensure_schema(conn, "public", roles.owner)
        await apply_schema_privileges(conn, "public", roles, options)
        await apply_default_privileges(conn, "public", roles, options)

    try:
        yield name, roles, options, server
    finally:
        await _teardown(pools, endpoint, name, roles)


async def test_managing_role_is_not_a_superuser(conn) -> None:
    """The whole suite is only meaningful under RDS-like privileges."""
    server = await describe_server(conn)
    assert not server.is_superuser, (
        "these tests must run as a non-superuser to reflect RDS; "
        f"current_user holds [{server.describe_privileges()}]"
    )
    assert server.can_create_db and server.can_create_role


async def test_database_is_owned_by_the_owner_group(pools, endpoint, provisioned) -> None:
    name, roles, _, _ = provisioned
    async with pools.connection(endpoint) as conn:
        state = await db.get_database(conn, name)
    assert state is not None
    # Ownership handoff to a group role is the step that needs the managing role
    # to be a member of it, which is why ensure_self_membership exists.
    assert state.owner == roles.owner


async def test_public_cannot_connect_after_revoke(pools, endpoint, provisioned) -> None:
    name, roles, _, _ = provisioned
    async with pools.connection(endpoint) as conn:
        for role, expected in (
            (roles.owner, True),
            (roles.read_write, True),
            (roles.read_only, True),
        ):
            granted = await fetch_scalar(
                conn,
                sql.SQL("SELECT has_database_privilege(%s, %s, 'CONNECT')"),
                (role, name),
            )
            assert granted is expected, f"{role} CONNECT"

        public_connect = await fetch_scalar(
            conn, sql.SQL("SELECT has_database_privilege('public', %s, 'CONNECT')"), (name,)
        )
    assert public_connect is False


async def test_only_owner_and_rw_hold_temp(pools, endpoint, provisioned) -> None:
    name, roles, _, _ = provisioned
    async with pools.connection(endpoint) as conn:
        results = {
            role: await fetch_scalar(
                conn,
                sql.SQL("SELECT has_database_privilege(%s, %s, 'TEMPORARY')"),
                (role, name),
            )
            for role in roles.all
        }
    assert results[roles.owner] is True
    assert results[roles.read_write] is True
    # Read-only deliberately cannot create temp tables.
    assert results[roles.read_only] is False


async def test_schema_privileges_match_the_model(pools, endpoint, provisioned) -> None:
    """Asserted on an operator-created schema, so it holds on every version.

    Whether `public` can be managed depends on the server version; that is
    covered separately by test_public_schema_ownership_depends_on_version.
    """
    name, roles, options, _ = provisioned
    async with pools.connection(endpoint, name) as conn:
        await db.ensure_schema(conn, "owned", roles.owner)
        await apply_schema_privileges(conn, "owned", roles, options)
        grants = await observed_schema_grants(conn, roles)
        public_create = await fetch_scalar(
            conn, sql.SQL("SELECT has_schema_privilege('public', 'owned', 'CREATE')")
        )

    entries = set(grants.get("owned", []))
    assert f"{roles.owner}:USAGE+CREATE" in entries
    assert f"{roles.read_write}:USAGE" in entries
    assert f"{roles.read_only}:USAGE" in entries
    assert public_create is False, "PUBLIC must not retain CREATE on a managed schema"


async def test_public_schema_ownership_depends_on_version(
    pools, endpoint, provisioned
) -> None:
    """Before PostgreSQL 15 a non-superuser cannot take over `public`.

    From 15 it belongs to pg_database_owner, which the owner group already is.
    The operator must report the difference rather than fail on it.
    """
    name, roles, _, server = provisioned
    async with pools.connection(endpoint, name) as conn:
        outcome = await db.ensure_schema(conn, "public", roles.owner)

    if server.public_schema_is_locked_down:
        assert outcome in {
            db.SchemaOutcome.UNCHANGED,
            db.SchemaOutcome.OWNER_CHANGED,
        }
    else:
        assert outcome is db.SchemaOutcome.OWNER_DENIED


async def test_rw_can_write_and_ro_can_only_read(pools, endpoint, provisioned) -> None:
    """The core promise: a table created by the owner group is usable by both."""
    name, roles, _, _ = provisioned
    async with pools.connection(endpoint, name) as conn:
        # Create as the owner group, which is what setRoleForOwners arranges
        # for real login roles.
        await execute(conn, sql.SQL("SET ROLE {}").format(sql.Identifier(roles.owner)))
        await execute(conn, sql.SQL("CREATE TABLE public.widgets (id int primary key)"))
        await execute(conn, sql.SQL("RESET ROLE"))

        checks = {}
        for role in (roles.read_write, roles.read_only):
            for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                checks[(role, privilege)] = await fetch_scalar(
                    conn,
                    sql.SQL("SELECT has_table_privilege(%s, 'public.widgets', %s)"),
                    (role, privilege),
                )

    assert all(checks[(roles.read_write, p)] for p in ("SELECT", "INSERT", "UPDATE", "DELETE"))
    assert checks[(roles.read_only, "SELECT")] is True
    assert not any(checks[(roles.read_only, p)] for p in ("INSERT", "UPDATE", "DELETE"))


async def test_tables_created_after_the_grant_are_still_reachable(
    pools, endpoint, provisioned
) -> None:
    """This is what DEFAULT PRIVILEGES buy: no re-granting for future tables."""
    name, roles, _, _ = provisioned
    async with pools.connection(endpoint, name) as conn:
        await execute(conn, sql.SQL("SET ROLE {}").format(sql.Identifier(roles.owner)))
        await execute(conn, sql.SQL("CREATE TABLE public.later (id int)"))
        await execute(conn, sql.SQL("CREATE SEQUENCE public.later_seq"))
        await execute(conn, sql.SQL("RESET ROLE"))

        rw_select = await fetch_scalar(
            conn,
            sql.SQL("SELECT has_table_privilege(%s, 'public.later', 'SELECT')"),
            (roles.read_write,),
        )
        ro_select = await fetch_scalar(
            conn,
            sql.SQL("SELECT has_table_privilege(%s, 'public.later', 'SELECT')"),
            (roles.read_only,),
        )
        rw_seq = await fetch_scalar(
            conn,
            sql.SQL("SELECT has_sequence_privilege(%s, 'public.later_seq', 'USAGE')"),
            (roles.read_write,),
        )
        ro_seq_usage = await fetch_scalar(
            conn,
            sql.SQL("SELECT has_sequence_privilege(%s, 'public.later_seq', 'USAGE')"),
            (roles.read_only,),
        )

    assert rw_select is True
    assert ro_select is True
    assert rw_seq is True, "RW needs sequence USAGE to insert into a serial column"
    assert ro_seq_usage is False


async def test_adding_a_schema_later_grants_existing_roles(
    pools, endpoint, provisioned
) -> None:
    """The scenario from the requirements: a schema added after the fact.

    Reconciliation is a full convergence pass, so adding `audit` to the spec
    creates it and wires up all three group roles — including default
    privileges, so tables created in it afterwards are reachable too.
    """
    name, roles, options, _ = provisioned

    async with pools.connection(endpoint, name) as conn:
        # has_schema_privilege errors on a missing schema, so check existence.
        exists = await fetch_scalar(
            conn, sql.SQL("SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'audit')")
        )
        assert exists is False, "the schema must not exist before this reconcile"

        # --- what a later reconcile does, verbatim ---
        outcome = await db.ensure_schema(conn, "audit", roles.owner)
        await apply_schema_privileges(conn, "audit", roles, options)
        await apply_default_privileges(conn, "audit", roles, options)
        assert outcome is db.SchemaOutcome.CREATED

        # A table created in the new schema *after* the grants.
        await execute(conn, sql.SQL("SET ROLE {}").format(sql.Identifier(roles.owner)))
        await execute(conn, sql.SQL("CREATE TABLE audit.events (id int)"))
        await execute(conn, sql.SQL("RESET ROLE"))

        rw_usage = await fetch_scalar(
            conn,
            sql.SQL("SELECT has_schema_privilege(%s, 'audit', 'USAGE')"),
            (roles.read_write,),
        )
        ro_usage = await fetch_scalar(
            conn,
            sql.SQL("SELECT has_schema_privilege(%s, 'audit', 'USAGE')"),
            (roles.read_only,),
        )
        rw_insert = await fetch_scalar(
            conn,
            sql.SQL("SELECT has_table_privilege(%s, 'audit.events', 'INSERT')"),
            (roles.read_write,),
        )
        ro_insert = await fetch_scalar(
            conn,
            sql.SQL("SELECT has_table_privilege(%s, 'audit.events', 'INSERT')"),
            (roles.read_only,),
        )
        ro_select = await fetch_scalar(
            conn,
            sql.SQL("SELECT has_table_privilege(%s, 'audit.events', 'SELECT')"),
            (roles.read_only,),
        )
        schema_owner = await fetch_scalar(
            conn,
            sql.SQL(
                "SELECT r.rolname FROM pg_namespace n JOIN pg_roles r "
                "ON r.oid = n.nspowner WHERE n.nspname = 'audit'"
            ),
        )

    assert rw_usage is True
    assert ro_usage is True
    assert rw_insert is True
    assert ro_select is True
    assert ro_insert is False
    assert schema_owner == roles.owner


async def test_grants_are_idempotent(pools, endpoint, provisioned) -> None:
    """A periodic reconcile must be a no-op, not an error or a change."""
    name, roles, options, _ = provisioned
    async with pools.connection(endpoint, name) as conn:
        first = await observed_schema_grants(conn, roles)
        for _ in range(3):
            await apply_schema_privileges(conn, "public", roles, options)
            await apply_default_privileges(conn, "public", roles, options)
        second = await observed_schema_grants(conn, roles)
    assert first == second
