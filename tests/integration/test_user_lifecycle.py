"""Login roles, group membership, password handling and teardown, live."""

from __future__ import annotations

import pytest
from psycopg import sql

from pg_operator.k8s.secrets import generate_password
from pg_operator.postgres import database as db
from pg_operator.postgres import roles as pgroles
from pg_operator.postgres.connection import Endpoint, PoolRegistry
from pg_operator.postgres.privileges import (
    GrantOptions,
    GroupRoles,
    apply_default_privileges,
    apply_schema_privileges,
)
from pg_operator.postgres.server import describe_server
from pg_operator.postgres.sql import execute, fetch_scalar

from ..conftest import drop_database_hard, drop_roles_hard, requires_postgres

pytestmark = [requires_postgres, pytest.mark.asyncio]


@pytest.fixture
async def database(pools: PoolRegistry, endpoint: Endpoint, unique: str):
    """A provisioned database, as the database reconciler would leave it."""
    name = f"pgu_{unique}"
    roles = GroupRoles(
        owner=f"{name}_owner", read_write=f"{name}_rw", read_only=f"{name}_ro"
    )
    async with pools.connection(endpoint) as conn:
        server = await describe_server(conn)
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

    options = GrantOptions(
        public_schema_locked_by_default=server.public_schema_is_locked_down
    )
    async with pools.connection(endpoint, name) as conn:
        await db.ensure_schema(conn, "public", roles.owner)
        await apply_schema_privileges(conn, "public", roles, options)
        await apply_default_privileges(conn, "public", roles, options)

    try:
        yield name, roles, server
    finally:
        await drop_database_hard(pools, endpoint, name)
        await drop_roles_hard(pools, endpoint, roles.all)


@pytest.fixture
async def user(pools: PoolRegistry, endpoint: Endpoint, unique: str):
    """A login role, cleaned up afterwards regardless of what the test did."""
    username = f"pgu_user_{unique}"
    password = generate_password(32)
    async with pools.connection(endpoint) as conn:
        await pgroles.ensure_login_role(
            conn,
            username,
            password=password,
            attributes={"login": True, "inherit": True},
        )
    try:
        yield username, password
    finally:
        await drop_roles_hard(pools, endpoint, [username])


async def test_login_role_is_created_with_expected_attributes(
    pools, endpoint, user
) -> None:
    username, _ = user
    async with pools.connection(endpoint) as conn:
        state = await pgroles.get_role(conn, username)
    assert state is not None
    assert state.can_login is True
    assert state.inherit is True, "membership conveys nothing without INHERIT"
    assert state.create_db is False


async def test_ensure_login_role_is_idempotent(pools, endpoint, user) -> None:
    username, password = user
    async with pools.connection(endpoint) as conn:
        created = await pgroles.ensure_login_role(
            conn,
            username,
            password=password,
            attributes={"login": True, "inherit": True},
        )
    # Second call must converge, not recreate.
    assert created is False


async def test_generated_password_actually_authenticates(
    pools, endpoint, user, settings
) -> None:
    """Proof the credential written to the Secret is the one the server accepts."""
    username, password = user
    as_user = Endpoint(
        instance="test-asuser",
        host=endpoint.host,
        port=endpoint.port,
        username=username,
        password=password,
        maintenance_database=endpoint.maintenance_database,
        ssl_mode=endpoint.ssl_mode,
    )
    registry = PoolRegistry(settings)
    try:
        async with registry.connection(as_user) as conn:
            who = await fetch_scalar(conn, sql.SQL("SELECT current_user"))
    finally:
        await registry.close_all()
    assert who == username


async def test_password_verification_degrades_when_pg_authid_is_unreadable(
    pools, endpoint, user
) -> None:
    """On RDS the managing role cannot read pg_authid, so the check must abstain.

    Returning None (rather than guessing False) is what keeps a reconcile from
    rewriting a working credential it cannot verify. The verification logic
    itself is covered by the unit tests in tests/test_roles.py.
    """
    username, password = user
    async with pools.connection(endpoint) as conn:
        readable = await pgroles._stored_verifier(conn, username)
        verdict = await pgroles.password_matches(conn, username, password)
    assert readable is None, "expected pg_authid to be unreadable as a non-superuser"
    assert verdict is None


async def test_membership_grants_the_group_privileges(
    pools, endpoint, database, user
) -> None:
    name, roles, _ = database
    username, _ = user

    async with pools.connection(endpoint) as conn:
        await pgroles.grant_membership(conn, roles.read_write, username)
        held = await pgroles.memberships(conn, username)
    assert roles.read_write in held

    async with pools.connection(endpoint, name) as conn:
        await execute(conn, sql.SQL("SET ROLE {}").format(sql.Identifier(roles.owner)))
        await execute(conn, sql.SQL("CREATE TABLE public.items (id int)"))
        await execute(conn, sql.SQL("RESET ROLE"))
        # Privileges reach the user through the group, never directly.
        can_insert = await fetch_scalar(
            conn,
            sql.SQL("SELECT has_table_privilege(%s, 'public.items', 'INSERT')"),
            (username,),
        )
        direct = await fetch_scalar(
            conn,
            sql.SQL(
                "SELECT count(*) FROM information_schema.role_table_grants "
                "WHERE grantee = %s AND table_name = 'items'"
            ),
            (username,),
        )
    assert can_insert is True
    assert direct == 0, "the user must hold no direct table grants"


async def test_downgrading_rw_to_ro_swaps_membership(
    pools, endpoint, database, user
) -> None:
    """An access-level change is two membership statements and no object work."""
    name, roles, _ = database
    username, _ = user

    async with pools.connection(endpoint) as conn:
        await pgroles.grant_membership(conn, roles.read_write, username)
    async with pools.connection(endpoint, name) as conn:
        await execute(conn, sql.SQL("SET ROLE {}").format(sql.Identifier(roles.owner)))
        await execute(conn, sql.SQL("CREATE TABLE public.notes (id int)"))
        await execute(conn, sql.SQL("RESET ROLE"))
        before = await fetch_scalar(
            conn,
            sql.SQL("SELECT has_table_privilege(%s, 'public.notes', 'INSERT')"),
            (username,),
        )
    assert before is True

    async with pools.connection(endpoint) as conn:
        await pgroles.revoke_membership(conn, roles.read_write, username)
        await pgroles.grant_membership(conn, roles.read_only, username)
        held = await pgroles.memberships(conn, username)

    async with pools.connection(endpoint, name) as conn:
        can_insert = await fetch_scalar(
            conn,
            sql.SQL("SELECT has_table_privilege(%s, 'public.notes', 'INSERT')"),
            (username,),
        )
        can_select = await fetch_scalar(
            conn,
            sql.SQL("SELECT has_table_privilege(%s, 'public.notes', 'SELECT')"),
            (username,),
        )
    assert roles.read_only in held and roles.read_write not in held
    assert can_insert is False
    assert can_select is True


async def test_owner_members_create_objects_as_the_group(
    pools, endpoint, database, user
) -> None:
    """setRoleForOwners: without it, RW cannot see tables an owner creates."""
    name, roles, _ = database
    username, password = user

    async with pools.connection(endpoint) as conn:
        await pgroles.grant_membership(conn, roles.owner, username)
        await pgroles.set_default_role_in_database(conn, username, name, roles.owner)
        observed = await pgroles.get_role_parameters(conn, username, name)
    assert observed.get("role") == roles.owner

    # Connect as the login role and create a table, as a migration job would.
    as_user = Endpoint(
        instance="test-owner",
        host=endpoint.host,
        port=endpoint.port,
        username=username,
        password=password,
        maintenance_database=name,
        ssl_mode=endpoint.ssl_mode,
    )
    registry = PoolRegistry()
    try:
        async with registry.connection(as_user, name) as conn:
            await execute(conn, sql.SQL("CREATE TABLE public.migrated (id int)"))
    finally:
        await registry.close_all()

    async with pools.connection(endpoint, name) as conn:
        owner = await fetch_scalar(
            conn,
            sql.SQL(
                "SELECT r.rolname FROM pg_class c JOIN pg_roles r ON r.oid = c.relowner "
                "WHERE c.relname = 'migrated'"
            ),
        )
        rw_select = await fetch_scalar(
            conn,
            sql.SQL("SELECT has_table_privilege(%s, 'public.migrated', 'SELECT')"),
            (roles.read_write,),
        )

    assert owner == roles.owner, "the table must belong to the group, not the login role"
    # Which is exactly what makes the group's DEFAULT PRIVILEGES apply.
    assert rw_select is True


async def test_teardown_drops_the_role_and_its_memberships(
    pools, endpoint, database, user
) -> None:
    name, roles, _ = database
    username, _ = user

    async with pools.connection(endpoint) as conn:
        await pgroles.grant_membership(conn, roles.read_write, username)
        await pgroles.set_default_role_in_database(conn, username, name, roles.owner)

    _, _, server = database

    # Mirrors teardown_user under a DROP policy, in the same order. The
    # membership grant is required: DROP OWNED BY needs the privileges of the
    # role being cleared, which creating it does not confer on PostgreSQL 16+.
    async with pools.connection(endpoint) as conn:
        await pgroles.ensure_self_membership(
            conn, username, per_grant_options=server.supports_per_grant_options
        )
    async with pools.connection(endpoint, name) as conn:
        await pgroles.drop_owned(conn, username)
    async with pools.connection(endpoint) as conn:
        await pgroles.reset_default_role_in_database(conn, username, name)
        revoked = await pgroles.revoke_all_memberships(conn, username)
        await pgroles.drop_role(conn, username)
        still_there = await pgroles.role_exists(conn, username)
        leftover = await pgroles.get_role_parameters(conn, username, name)

    assert roles.read_write in revoked
    assert still_there is False
    assert leftover == {}
