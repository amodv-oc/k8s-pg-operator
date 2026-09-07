"""Retention behaviour: RETAIN leaves real objects alone, DROP removes them."""

from __future__ import annotations

import pytest
from psycopg import sql

from pg_operator.postgres import database as db
from pg_operator.postgres import roles as pgroles
from pg_operator.postgres.connection import Endpoint, PoolRegistry
from pg_operator.postgres.privileges import GroupRoles
from pg_operator.postgres.server import describe_server
from pg_operator.postgres.sql import execute, fetch_scalar

from ..conftest import drop_database_hard, drop_roles_hard, requires_postgres

pytestmark = [requires_postgres, pytest.mark.asyncio]


async def _make_database(pools: PoolRegistry, endpoint: Endpoint, name: str):
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
    async with pools.connection(endpoint, name) as conn:
        await db.ensure_schema(conn, "audit", roles.owner)
        await execute(conn, sql.SQL("CREATE TABLE audit.trail (id int)"))
    return roles, server


async def _force_cleanup(pools, endpoint, name, roles) -> None:
    await drop_database_hard(pools, endpoint, name)
    await drop_roles_hard(pools, endpoint, roles.all)


async def test_drop_policy_removes_the_database_and_its_roles(
    pools, endpoint, unique
) -> None:
    name = f"pgr_drop_{unique}"
    roles, server = await _make_database(pools, endpoint, name)
    try:
        # Hold an open session, as a running application would.
        holder = PoolRegistry()
        async with holder.connection(endpoint, name) as conn:
            await fetch_scalar(conn, sql.SQL("SELECT 1"))

            # --- what teardown_database does under DROP ---
            await pools.close_database(endpoint.instance, name)
            async with pools.connection(endpoint) as admin:
                terminated, _ = await db.terminate_connections(admin, name)
                await db.drop_database(
                    admin, name, force=server.supports_drop_database_force
                )
                gone = await db.get_database(admin, name)
                dropped = []
                for role in roles.all:
                    if not await pgroles.owned_objects_exist(admin, role):
                        await pgroles.drop_role(admin, role)
                        dropped.append(role)
                remaining = [
                    role for role in roles.all if await pgroles.role_exists(admin, role)
                ]
        await holder.close_all()

        assert terminated >= 1, "an open session must be terminated, not block the drop"
        assert gone is None
        assert sorted(dropped) == sorted(roles.all)
        assert remaining == []
    finally:
        await _force_cleanup(pools, endpoint, name, roles)


async def test_retain_policy_leaves_everything_in_place(pools, endpoint, unique) -> None:
    """RETAIN is the default, and must not touch the server at all."""
    name = f"pgr_keep_{unique}"
    roles, _ = await _make_database(pools, endpoint, name)
    try:
        # teardown_database under RETAIN issues no statements; assert the
        # objects are still exactly as provisioned.
        async with pools.connection(endpoint) as conn:
            state = await db.get_database(conn, name)
            roles_present = [
                role for role in roles.all if await pgroles.role_exists(conn, role)
            ]
        async with pools.connection(endpoint, name) as conn:
            rows = await fetch_scalar(
                conn,
                sql.SQL(
                    "SELECT count(*) FROM pg_tables "
                    "WHERE schemaname = 'audit' AND tablename = 'trail'"
                ),
            )
        assert state is not None and state.owner == roles.owner
        assert sorted(roles_present) == sorted(roles.all)
        assert rows == 1
    finally:
        await _force_cleanup(pools, endpoint, name, roles)


async def test_a_role_owning_another_database_is_not_dropped(
    pools, endpoint, unique
) -> None:
    """Guards a shared-role setup from losing a role another database needs."""
    name = f"pgr_a_{unique}"
    other = f"pgr_b_{unique}"
    roles, _ = await _make_database(pools, endpoint, name)
    try:
        async with pools.connection(endpoint) as conn:
            # A second database owned by the same role.
            await db.create_database(conn, other, owner=roles.owner)
            still_owns = await pgroles.owned_objects_exist(conn, roles.owner)
            rw_owns = await pgroles.owned_objects_exist(conn, roles.read_write)
        assert still_owns is True, "the owner role must be reported as still in use"
        assert rw_owns is False
    finally:
        await drop_database_hard(pools, endpoint, other)
        await _force_cleanup(pools, endpoint, name, roles)


async def test_orphaned_schema_is_dropped_only_under_drop(
    pools, endpoint, unique
) -> None:
    """A schema removed from the spec: retained by default, dropped on request."""
    name = f"pgr_orph_{unique}"
    roles, _ = await _make_database(pools, endpoint, name)
    try:
        async with pools.connection(endpoint, name) as conn:
            before = {s.name for s in await db.list_schemas(conn)}
            assert "audit" in before

            # RETAIN: the reconciler records the orphan and drops nothing.
            after_retain = {s.name for s in await db.list_schemas(conn)}
            assert "audit" in after_retain

            # DROP: CASCADE, because the schema holds a table.
            assert await db.schema_is_empty(conn, "audit") is False
            await db.drop_schema(conn, "audit", cascade=True)
            after_drop = {s.name for s in await db.list_schemas(conn)}
        assert "audit" not in after_drop
    finally:
        await _force_cleanup(pools, endpoint, name, roles)


async def test_extension_lifecycle(pools, endpoint, unique) -> None:
    name = f"pgr_ext_{unique}"
    roles, _ = await _make_database(pools, endpoint, name)
    try:
        async with pools.connection(endpoint, name) as conn:
            available = await db.available_extensions(conn)
            if "citext" not in available:
                pytest.skip("citext is not available on this server")

            baseline = {e.name for e in await db.list_extensions(conn)}
            # plpgsql ships in every database and must never count as drift.
            assert "plpgsql" in baseline

            await db.create_extension(conn, "citext", schema="public")
            installed = {e.name for e in await db.list_extensions(conn)}
            assert "citext" in installed

            # Idempotent: a second reconcile must not fail.
            await db.create_extension(conn, "citext", schema="public")

            await db.drop_extension(conn, "citext", cascade=True)
            final = {e.name for e in await db.list_extensions(conn)}
        assert "citext" not in final
    finally:
        await _force_cleanup(pools, endpoint, name, roles)


async def test_database_parameters_converge_and_reset(pools, endpoint, unique) -> None:
    name = f"pgr_param_{unique}"
    roles, _ = await _make_database(pools, endpoint, name)
    try:
        async with pools.connection(endpoint) as conn:
            observed = await db.get_database_parameters(conn, name)
            outcome = await db.apply_database_parameters(
                conn, name, {"search_path": "public, audit"}, observed
            )
            assert outcome.changes
            assert outcome.denied == []

            observed = await db.get_database_parameters(conn, name)
            assert observed.get("search_path") == "public, audit"

            # Re-applying the same value must be a no-op.
            repeat = await db.apply_database_parameters(
                conn, name, {"search_path": "public, audit"}, observed
            )
            assert repeat.changes == []
            assert repeat.denied == []

            # Removing it from the spec resets it.
            resets = await db.apply_database_parameters(conn, name, {}, observed)
            assert any("reset" in change for change in resets.changes)
            assert await db.get_database_parameters(conn, name) == {}
    finally:
        await _force_cleanup(pools, endpoint, name, roles)


async def test_superuser_only_parameter_is_reported_not_raised(
    pools, endpoint, unique
) -> None:
    """A GUC the managing role may not set must not abort the pass.

    ``log_min_duration_statement`` is superuser-only on a stock server. RDS
    grants its master user the right to set it, and PostgreSQL 15 added
    ``GRANT SET ON PARAMETER`` as the stock equivalent - so the test first
    establishes that this server really does refuse the managing role, and
    skips rather than asserting something untrue if it does not.
    """
    name = f"pgr_denied_{unique}"
    async with pools.connection(endpoint) as conn:
        server = await describe_server(conn)
        if server.is_superuser:
            pytest.skip("a superuser can set any parameter")
        if server.major_version >= 15 and await fetch_scalar(
            conn,
            sql.SQL(
                "SELECT has_parameter_privilege("
                "current_user, 'log_min_duration_statement', 'SET')"
            ),
        ):
            pytest.skip(
                "this server grants SET on log_min_duration_statement to the "
                "managing role, so there is nothing to refuse"
            )

    roles, _ = await _make_database(pools, endpoint, name)
    try:
        async with pools.connection(endpoint) as conn:
            observed = await db.get_database_parameters(conn, name)
            outcome = await db.apply_database_parameters(
                conn,
                name,
                {"log_min_duration_statement": "500", "search_path": "public"},
                observed,
            )
            assert len(outcome.denied) == 1
            assert "log_min_duration_statement" in outcome.denied[0]
            # The settable parameter alongside it still went through.
            assert outcome.changes == ["set search_path=public"]
            assert (
                await db.get_database_parameters(conn, name)
            ).get("search_path") == "public"
    finally:
        await _force_cleanup(pools, endpoint, name, roles)
