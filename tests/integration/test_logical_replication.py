"""The logical replication probe, against a live RDS-like server.

The assertions compare the probe with what the server itself says rather than
with fixed values, so they hold under any ``wal_level`` and on every version -
CI's single-server job runs a default ``replica`` server, `make test-setup` a
``logical`` one.
"""

from __future__ import annotations

import pytest
from psycopg import sql

from pg_operator.postgres.replication import describe_logical_replication
from pg_operator.postgres.server import describe_server
from pg_operator.postgres.sql import fetch_scalar

from ..conftest import requires_postgres

pytestmark = [requires_postgres, pytest.mark.asyncio]


async def test_probe_reports_the_live_settings(conn) -> None:
    server = await describe_server(conn)
    info = await describe_logical_replication(conn, server)

    assert info.wal_level == await fetch_scalar(conn, sql.SQL("SHOW wal_level"))
    assert info.max_replication_slots == int(
        await fetch_scalar(conn, sql.SQL("SHOW max_replication_slots"))
    )
    assert info.replication_slots_in_use == await fetch_scalar(
        conn, sql.SQL("SELECT count(*) FROM pg_replication_slots")
    )
    assert info.major_version == server.major_version
    # Stock PostgreSQL has no rds.logical_replication, so remedies name wal_level.
    assert info.is_rds is False


async def test_subscription_privilege_follows_pg_create_subscription(conn) -> None:
    server = await describe_server(conn)
    if server.is_superuser:
        pytest.skip("a superuser may always create subscriptions")
    info = await describe_logical_replication(conn, server)

    holds_role = server.major_version >= 16 and await fetch_scalar(
        conn,
        sql.SQL("SELECT pg_has_role(current_user, 'pg_create_subscription', 'USAGE')"),
    )
    if holds_role:
        assert info.create_subscription == "Granted"
    elif server.is_rds_superuser:
        # RDS permits it without pg_create_subscription; the probe cannot see how.
        assert info.create_subscription == "Assumed"
    else:
        assert info.create_subscription == "Unavailable"


async def test_the_managing_role_cannot_confer_replication_itself(conn) -> None:
    """Like the RDS master user, it holds no REPLICATION attribute of its own.

    So the only way it can let a login stream is by granting rds_replication,
    which hack/rds-like.sql makes grantable as RDS does.
    """
    server = await describe_server(conn)
    if server.is_superuser:
        pytest.skip("a superuser may confer REPLICATION directly")
    assert not await fetch_scalar(
        conn, sql.SQL("SELECT rolreplication FROM pg_roles WHERE rolname = current_user")
    )
    info = await describe_logical_replication(conn, server)

    grantable = await fetch_scalar(
        conn,
        sql.SQL("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rds_replication')"),
    )
    assert info.replication_grant == ("rds_replication" if grantable else "none")
