"""The source side of a PostgresReplication, against two real servers.

What reaches the publication, what is held back and why, how the replication
login is made and removed, and that a converged pass changes nothing. The
subscription these feed is exercised in test_replication_subscription.py.
"""

from __future__ import annotations

import psycopg
import pytest
from psycopg import sql

from pg_operator.errors import ConfigurationError, ReconcileFailure, ReplicationRefused
from pg_operator.postgres import publication as pub
from pg_operator.postgres import subscription as sub
from pg_operator.postgres.connection import Endpoint
from pg_operator.postgres.sql import fetch_scalar
from pg_operator.reconcile import replication as rep
from pg_operator.reconcile.replication import (
    observe_from_status,
    ownership_marker,
    teardown_replication,
)

from ..conftest import requires_replication_harness, superuser_execute
from .conftest import NS, OPERATOR_NS, World

pytestmark = [requires_replication_harness, pytest.mark.replication, pytest.mark.asyncio]


async def test_the_publication_carries_what_both_servers_can_take(world: World) -> None:
    status = await world.run()

    # Held-back tables are divergence the spec asks for but cannot have.
    assert status["phase"] == "Drifted"
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
    # Before the harness emulates rds_replication, the login holds membership
    # only: the attribute is RDS's to confer, never the operator's.
    with pytest.raises(ReplicationRefused):
        await world.run(emulate=False)
    login = world.status["source"]["login"]
    async with world.pools.connection(world.source) as conn:
        assert await fetch_scalar(
            conn, sql.SQL("SELECT rolreplication FROM pg_roles WHERE rolname = %s"), (login,)
        ) is False

    status = await world.run()
    assert status["created"] == {
        "login": True, "secret": True, "publication": True, "slot": True, "subscription": True,
    }
    assert set(status["grantedMemberships"]) == {"rds_replication", world.groups["readOnly"]}

    async with world.pools.connection(world.source) as conn:
        marker = await fetch_scalar(
            conn,
            sql.SQL("SELECT shobj_description(oid, 'pg_authid') FROM pg_roles WHERE rolname = %s"),
            (login,),
        )
        assert marker == ownership_marker(NS, world.replication["metadata"]["name"])

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


async def test_a_covering_index_does_not_widen_the_replica_identity(world: World) -> None:
    """INCLUDE columns are in pg_index.indkey but not in the replica identity.

    Counting them would let a filter on one past the gate, and the source would
    then refuse every UPDATE and DELETE on the table - the failure the gate is
    there to prevent.
    """
    table = (
        "CREATE TABLE public.covering (id int, region text, note text, "
        "PRIMARY KEY (id) INCLUDE (region))"
    )
    await world.on_source(table, as_owner=True)
    await world.on_target(table, as_owner=True)
    world.replication["spec"]["tables"].append(
        {
            "schema": "public",
            "name": "covering",
            "rowFilter": {"all": [{"column": "region", "op": "IsNotNull"}]},
        }
    )
    status = await world.run()
    assert world.excluded()["public.covering"] == "RowFilterNotCoveredByIdentity"
    assert "public.covering" not in status["publishedTables"]
    # Held back, so the source's own writes still work.
    await world.on_source(
        "INSERT INTO public.covering VALUES (1, 'AU', 'x')",
        "UPDATE public.covering SET note = 'y'",
        as_owner=True,
    )

    # A column list that leaves the INCLUDE column out is not missing the
    # identity either, so this one is published and its rows copied.
    world.replication["spec"]["tables"][-1] = {
        "schema": "public",
        "name": "covering",
        "columns": ["id", "note"],
    }
    status = await world.run()
    assert "public.covering" in status["publishedTables"]
    await world.on_source("UPDATE public.covering SET note = 'z'", as_owner=True)
    await world.eventually(
        "SELECT note FROM public.covering WHERE id = 1", "z", "the update to reach the target"
    )


async def test_a_target_that_cannot_identify_rows_is_held_back(world: World) -> None:
    """Apply is sequential: one such table stops every table the subscription carries."""
    await world.on_source("CREATE TABLE public.keyed (id int PRIMARY KEY, v text)", as_owner=True)
    await world.on_target("CREATE TABLE public.keyed (id int, v text)", as_owner=True)
    status = await world.run()
    assert world.excluded()["public.keyed"] == "TargetNoReplicaIdentity"
    assert "public.keyed" not in status["publishedTables"]

    await world.on_target("ALTER TABLE public.keyed ADD PRIMARY KEY (id)", as_owner=True)
    status = await world.run()
    assert "public.keyed" in status["publishedTables"]


async def test_a_target_identity_the_publisher_never_sends_is_held_back(world: World) -> None:
    await world.on_source(
        "CREATE TABLE public.wide (id int PRIMARY KEY, region text NOT NULL, v text)", as_owner=True
    )
    await world.on_target(
        "CREATE TABLE public.wide (id int, region text NOT NULL, v text, PRIMARY KEY (id, region))",
        as_owner=True,
    )
    status = await world.run()
    assert world.excluded()["public.wide"] == "TargetIdentityNotSent"
    assert "public.wide" not in status["publishedTables"]


async def test_a_not_null_target_column_the_publication_cannot_fill_is_held_back(
    world: World,
) -> None:
    await world.on_source("CREATE TABLE public.audited (id int PRIMARY KEY, v text)", as_owner=True)
    await world.on_target(
        "CREATE TABLE public.audited (id int PRIMARY KEY, v text, tenant text NOT NULL)",
        as_owner=True,
    )
    status = await world.run()
    assert world.excluded()["public.audited"] == "TargetColumnRequired"
    assert "public.audited" not in status["publishedTables"]

    # A default is all the server needs to fill the column in by itself.
    await world.on_target(
        "ALTER TABLE public.audited ALTER COLUMN tenant SET DEFAULT 'house'", as_owner=True
    )
    status = await world.run()
    assert "public.audited" in status["publishedTables"]
    await world.on_source("INSERT INTO public.audited VALUES (1, 'a')", as_owner=True)
    await world.eventually(
        "SELECT tenant FROM public.audited WHERE id = 1", "house", "the insert to reach the target"
    )


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
        # Recorded false rather than left out: status is a JSON merge patch, so
        # a key dropped here would keep an earlier pass's `true` and teardown
        # would drop a login and a Secret the operator never made.
        assert status["created"]["login"] is False
        assert status["created"]["secret"] is False

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


async def test_switching_to_a_provided_login_spares_it_and_removes_the_generated_one(
    world: World, unique: str
) -> None:
    """credentialsSecretRef can be added to a live replication; both logins must be handled.

    The provided role and its Secret are the user's, whatever an earlier pass
    with a generated login recorded - teardown must not reach for them - and
    the generated pair this one replaces has to go rather than linger with a
    live password and a grant on the source.
    """
    status = await world.run()
    generated, secret = status["source"]["login"], status["credentialsSecret"]
    assert status["created"]["login"] and status["created"]["secret"]
    assert world.k8s.core.get(secret["namespace"], secret["name"]) is not None

    login, password = f"rs_swap_{unique}", f"swap-{unique}-password"
    superuser_execute(f"CREATE ROLE \"{login}\" LOGIN REPLICATION PASSWORD '{password}'")
    try:
        world.k8s.core.seed(NS, "swap-login", {"username": login, "password": password})
        world.replication["spec"]["source"]["credentialsSecretRef"] = {"name": "swap-login"}
        status = await world.run()

        assert status["source"]["login"] == login
        assert status["created"]["login"] is False and status["created"]["secret"] is False
        # Only what this pass granted the new login, not what the old one held.
        assert status["grantedMemberships"] == [world.groups["readOnly"]]
        assert world.k8s.core.get(secret["namespace"], secret["name"]) is None
        async with world.pools.connection(world.source) as conn:
            assert not await fetch_scalar(
                conn, sql.SQL("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = %s)"), (generated,)
            )
        # Streaming survived the swap.
        await world.on_source("INSERT INTO audit.events VALUES (9, 'swapped')", as_owner=True)
        await world.eventually(
            "SELECT kind FROM audit.events WHERE id = 9", "swapped", "the insert to reach the target"
        )

        await teardown_replication(world.ctx, world.replication)
        world.replication.pop("status")
        async with world.pools.connection(world.source) as conn:
            assert await fetch_scalar(
                conn, sql.SQL("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = %s)"), (login,)
            )
        assert world.k8s.core.get(NS, "swap-login") is not None
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
    moment it exists. Here the source refuses the new login a walsender - the
    harness has not yet emulated rds_replication - after the publication, login
    and Secret already exist.
    """
    with pytest.raises(ReplicationRefused):
        await world.run(emulate=False)

    recorded = world.replication["status"]
    # The slot is recorded before it is made, and making it is what failed.
    assert recorded["created"] == {"publication": True, "login": True, "secret": True, "slot": True}
    assert set(recorded["grantedMemberships"]) == {"rds_replication", world.groups["readOnly"]}
    # A slot recorded but never made is not one that was lost.
    assert await observe_from_status(world.ctx, world.replication) is None
    message = await teardown_replication(world.ctx, world.replication)
    world.replication.pop("status")
    assert recorded["source"]["publication"] in message
    assert recorded["source"]["login"] in message
    assert world.k8s.core.get(OPERATOR_NS, recorded["credentialsSecret"]["name"]) is None


def _fail_on(module: object, statement: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``module``'s ``execute`` fail on one statement, as a dropped connection would."""
    real = module.execute  # type: ignore[attr-defined]

    async def failing(conn, query, params=None, *, describe=None):  # type: ignore[no-untyped-def]
        if statement in query.as_string(conn):
            raise ReconcileFailure("server closed the connection unexpectedly")
        return await real(conn, query, params, describe=describe)

    monkeypatch.setattr(module, "execute", failing)


async def _exists(world: World, catalog: str, column: str, name: str, *, database: str | None = None) -> bool:
    async with world.pools.connection(world.source, database) as conn:
        return bool(
            await fetch_scalar(
                conn,
                sql.SQL("SELECT EXISTS (SELECT 1 FROM {} WHERE {} = %s)").format(
                    sql.Identifier(catalog), sql.Identifier(column)
                ),
                (name,),
            )
        )


@pytest.mark.parametrize(
    ("kind", "statement"),
    [("publication", "COMMENT ON PUBLICATION"), ("login", "COMMENT ON ROLE")],
)
async def test_an_object_never_exists_without_its_marker(
    world: World, monkeypatch: pytest.MonkeyPatch, kind: str, statement: str
) -> None:
    """Created and marked in one transaction, or not at all.

    An unmarked object of this replication's name is refused as someone
    else's, so one left behind by a failure between the two statements would
    fail every later pass - and never be torn down.
    """
    from pg_operator.naming import replication_names

    names = replication_names(NS, world.replication["metadata"]["name"])
    _fail_on(pub if kind == "publication" else rep, statement, monkeypatch)
    with pytest.raises(ReconcileFailure, match="closed the connection"):
        await world.run()
    monkeypatch.undo()

    if kind == "publication":
        assert not await _exists(world, "pg_publication", "pubname", names.publication, database=world.source_db)
    else:
        assert not await _exists(world, "pg_roles", "rolname", names.login)
    status = await world.run()
    assert status["created"]["publication"] and status["created"]["login"]


async def test_a_login_from_a_pass_that_failed_after_making_it_is_torn_down(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The login is recorded before it is made, not after everything else about it succeeded."""

    async def failing_apply_secret(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise ReconcileFailure("the API server is unavailable")

    monkeypatch.setattr(rep, "apply_secret", failing_apply_secret)
    with pytest.raises(ReconcileFailure, match="unavailable"):
        await world.run()
    monkeypatch.undo()

    recorded = world.replication["status"]
    login = recorded["source"]["login"]
    assert recorded["created"]["login"] is True and login
    assert await _exists(world, "pg_roles", "rolname", login)
    await teardown_replication(world.ctx, world.replication)
    world.replication.pop("status")
    assert not await _exists(world, "pg_roles", "rolname", login)


async def test_a_recreated_publication_does_not_blank_what_status_recorded(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recording one object must not erase another's record - teardown needs them all."""
    await world.run()
    login = world.status["source"]["login"]
    oid = world.status["target"]["subscriptionOid"]
    await world.on_source(f"DROP PUBLICATION {world.status['source']['publication']}")

    async def failing_apply_secret(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise ReconcileFailure("the API server is unavailable")

    monkeypatch.setattr(rep, "apply_secret", failing_apply_secret)
    with pytest.raises(ReconcileFailure, match="unavailable"):
        await world.run()
    monkeypatch.undo()
    assert world.status["source"].get("login") == login
    assert world.status["target"].get("subscriptionOid") == oid


async def test_a_converged_pass_reads_the_publication_once(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It runs every minute for every replication: once, plus a read-back only after a change."""
    await world.run()
    reads = 0
    real = pub.get_publication

    async def counting(conn, name):  # type: ignore[no-untyped-def]
        nonlocal reads
        reads += 1
        return await real(conn, name)

    patches = 0
    real_patch = rep.patch_status

    async def counting_patches(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal patches
        patches += 1
        return await real_patch(*args, **kwargs)

    monkeypatch.setattr(pub, "get_publication", counting)
    monkeypatch.setattr(rep, "patch_status", counting_patches)
    await world.run()
    assert world.last_changes == []
    assert reads == 1
    # Recording is part of creating: a pass that creates nothing records nothing.
    assert patches == 0

    reads = 0
    await world.on_target("CREATE TABLE public.only_source (id int PRIMARY KEY)", as_owner=True)
    status = await world.run()
    assert "added public.only_source" in world.last_changes
    assert "public.only_source" in status["publishedTables"]
    assert reads == 2


# --- swapping one login for another ---------------------------------------------
#
# Adding, removing or repointing source.credentialsSecretRef swaps the login.
# The new one is recorded as soon as it is in use; the one it replaces must
# stay known to status until it is gone, whichever pass that is.


def _byo_login(world: World, unique: str) -> str:
    login, password = f"rs_byo_{unique}", f"byo-{unique}-password"
    superuser_execute(f"CREATE ROLE \"{login}\" LOGIN REPLICATION PASSWORD '{password}'")
    world.k8s.core.seed(NS, "byo-login", {"username": login, "password": password})
    return login


async def _fail_the_swap(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """A pass that records the new login, then fails before the old one is removed."""

    async def failing(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise ReconcileFailure("the target server is restarting")

    monkeypatch.setattr(sub, "set_connection", failing)
    with pytest.raises(ReconcileFailure, match="restarting"):
        await world.run()
    monkeypatch.undo()


async def _is_member(world: World, login: str, group: str) -> bool:
    async with world.pools.connection(world.source) as conn:
        return bool(
            await fetch_scalar(
                conn,
                sql.SQL(
                    "SELECT EXISTS (SELECT 1 FROM pg_auth_members m JOIN pg_roles g ON g.oid = m.roleid "
                    "JOIN pg_roles u ON u.oid = m.member WHERE u.rolname = %s AND g.rolname = %s)"
                ),
                (login, group),
            )
        )


async def test_a_login_replaced_by_a_failed_pass_is_removed_by_the_next(
    world: World, monkeypatch: pytest.MonkeyPatch, unique: str
) -> None:
    status = await world.run()
    generated, secret = status["source"]["login"], status["credentialsSecret"]
    byo = _byo_login(world, unique)
    try:
        world.replication["spec"]["source"]["credentialsSecretRef"] = {"name": "byo-login"}
        await _fail_the_swap(world, monkeypatch)
        assert world.status["source"]["login"] == byo

        await world.run()
        assert not await _exists(world, "pg_roles", "rolname", generated)
        assert world.k8s.core.get(secret["namespace"], secret["name"]) is None
        assert not world.status.get("supersededLogins")
        await world.run()
        assert world.last_changes == []
        # While the provided login it now streams as still exists.
        await teardown_replication(world.ctx, world.replication)
        world.replication.pop("status")
    finally:
        superuser_execute(f'DROP ROLE IF EXISTS "{byo}"')


async def test_teardown_removes_a_login_a_failed_pass_replaced(
    world: World, monkeypatch: pytest.MonkeyPatch, unique: str
) -> None:
    status = await world.run()
    generated, secret = status["source"]["login"], status["credentialsSecret"]
    byo = _byo_login(world, unique)
    try:
        world.replication["spec"]["source"]["credentialsSecretRef"] = {"name": "byo-login"}
        await _fail_the_swap(world, monkeypatch)

        await teardown_replication(world.ctx, world.replication)
        world.replication.pop("status")
        assert not await _exists(world, "pg_roles", "rolname", generated)
        assert world.k8s.core.get(secret["namespace"], secret["name"]) is None
        # The provided login stays, without what the operator granted it.
        assert await _exists(world, "pg_roles", "rolname", byo)
        assert not await _is_member(world, byo, world.groups["readOnly"])
        assert world.k8s.core.get(NS, "byo-login") is not None
    finally:
        superuser_execute(f'DROP ROLE IF EXISTS "{byo}"')


async def test_a_provided_login_a_failed_pass_replaced_loses_only_the_operators_grants(
    world: World, monkeypatch: pytest.MonkeyPatch, unique: str
) -> None:
    byo = _byo_login(world, unique)
    own = f"rs_own_{unique}"
    superuser_execute(f'CREATE ROLE "{own}"')
    superuser_execute(f'GRANT "{own}" TO "{byo}"')
    try:
        world.replication["spec"]["source"]["credentialsSecretRef"] = {"name": "byo-login"}
        await world.run()
        assert await _is_member(world, byo, world.groups["readOnly"])

        del world.replication["spec"]["source"]["credentialsSecretRef"]
        await _fail_the_swap(world, monkeypatch)
        await world.run()

        assert await _exists(world, "pg_roles", "rolname", byo)
        assert not await _is_member(world, byo, world.groups["readOnly"])
        assert await _is_member(world, byo, own)
        assert world.status["created"]["login"] is True
        assert not world.status.get("supersededLogins")
    finally:
        superuser_execute(f'DROP ROLE IF EXISTS "{byo}"')
        superuser_execute(f'DROP ROLE IF EXISTS "{own}"')


async def test_a_login_swapped_back_after_a_failed_pass_is_kept(
    world: World, monkeypatch: pytest.MonkeyPatch, unique: str
) -> None:
    status = await world.run()
    generated, secret = status["source"]["login"], status["credentialsSecret"]
    byo = _byo_login(world, unique)
    try:
        world.replication["spec"]["source"]["credentialsSecretRef"] = {"name": "byo-login"}
        await _fail_the_swap(world, monkeypatch)

        del world.replication["spec"]["source"]["credentialsSecretRef"]
        status = await world.run()
        assert status["source"]["login"] == generated
        assert status["created"]["login"] is True and status["created"]["secret"] is True
        assert await _exists(world, "pg_roles", "rolname", generated)
        assert world.k8s.core.get(secret["namespace"], secret["name"]) is not None
        # The provided login it briefly used is the one superseded now.
        assert not await _is_member(world, byo, world.groups["readOnly"])
        assert not status.get("supersededLogins")
        await world.on_source("INSERT INTO audit.events VALUES (5, 'back')", as_owner=True)
        await world.eventually("SELECT kind FROM audit.events WHERE id = 5", "back", "the insert")
    finally:
        superuser_execute(f'DROP ROLE IF EXISTS "{byo}"')


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



async def test_a_deferrable_primary_key_is_no_replica_identity(world: World) -> None:
    """PostgreSQL never uses a DEFERRABLE key as the identity, so published, this UPDATE would fail."""
    await world.on_source(
        "CREATE TABLE public.deferred (id int PRIMARY KEY DEFERRABLE, v text)", as_owner=True
    )
    await world.on_target("CREATE TABLE public.deferred (id int PRIMARY KEY, v text)", as_owner=True)
    status = await world.run()
    assert world.excluded()["public.deferred"] == "NoReplicaIdentity"
    assert "public.deferred" not in status["publishedTables"]
    await world.on_source(
        "INSERT INTO public.deferred VALUES (1, 'a')", "UPDATE public.deferred SET v = 'b'", as_owner=True
    )


async def test_a_deferrable_primary_key_on_the_target_identifies_no_row(world: World) -> None:
    """The apply worker passes over it too, and would stop the whole subscription."""
    await world.on_source("CREATE TABLE public.deferred (id int PRIMARY KEY, v text)", as_owner=True)
    await world.on_target(
        "CREATE TABLE public.deferred (id int PRIMARY KEY DEFERRABLE, v text)", as_owner=True
    )
    status = await world.run()
    assert world.excluded()["public.deferred"] == "TargetNoReplicaIdentity"
    assert "public.deferred" not in status["publishedTables"]


async def test_replica_identity_full_takes_no_column_list(world: World) -> None:
    """Even a list naming every column: the source would refuse the table's UPDATEs and DELETEs."""
    table = "CREATE TABLE public.wholerow (id int, v text)"
    await world.on_source(table, "ALTER TABLE public.wholerow REPLICA IDENTITY FULL", as_owner=True)
    await world.on_target(table, as_owner=True)
    world.replication["spec"]["tables"].append(
        {"schema": "public", "name": "wholerow", "columns": ["id", "v"]}
    )
    status = await world.run()
    assert world.excluded()["public.wholerow"] == "ColumnListMissesIdentity"
    assert "public.wholerow" not in status["publishedTables"]
    await world.on_source(
        "INSERT INTO public.wholerow VALUES (1, 'a')", "UPDATE public.wholerow SET v = 'b'", as_owner=True
    )

    world.replication["spec"]["tables"].pop()
    status = await world.run()
    assert "public.wholerow" in status["publishedTables"]
    await world.on_source("UPDATE public.wholerow SET v = 'c'", as_owner=True)


async def test_an_unlogged_partition_holds_its_table_back(world: World) -> None:
    """Its changes are never in the WAL, so they would never reach the target."""
    root = "CREATE TABLE public.metrics (id int, at date, PRIMARY KEY (id, at)) PARTITION BY RANGE (at)"
    leaf = "PARTITION OF public.metrics FOR VALUES FROM ('2026-01-01') TO ('2027-01-01')"
    await world.on_source(root, f"CREATE UNLOGGED TABLE public.metrics_2026 {leaf}", as_owner=True)
    await world.on_target(root, f"CREATE TABLE public.metrics_2026 {leaf}", as_owner=True)
    status = await world.run()
    assert world.excluded()["public.metrics"] == "NotPublishable"
    assert "public.metrics" not in status["publishedTables"]

    await world.on_source("ALTER TABLE public.metrics_2026 SET LOGGED", as_owner=True)
    status = await world.run()
    assert "public.metrics" in status["publishedTables"]


async def test_a_generated_column_in_a_full_identity_is_held_back_on_18(world: World) -> None:
    """PostgreSQL 18 refuses UPDATE and DELETE when the identity holds an unpublished generated column."""
    async with world.pools.connection(world.source) as conn:
        version = await fetch_scalar(conn, sql.SQL("SELECT current_setting('server_version_num')::int"))
    await world.on_source(
        "CREATE TABLE public.derived (id int, a int, g int GENERATED ALWAYS AS (a * 2) STORED)",
        "ALTER TABLE public.derived REPLICA IDENTITY FULL",
        as_owner=True,
    )
    await world.on_target("CREATE TABLE public.derived (id int PRIMARY KEY, a int, g int)", as_owner=True)
    status = await world.run()
    if version < 180000:
        # Older publishers have no such check, and send whole rows without it.
        assert "public.derived" in status["publishedTables"]
        return
    assert world.excluded()["public.derived"] == "GeneratedColumnInIdentity"
    assert "public.derived" not in status["publishedTables"]
    await world.on_source(
        "INSERT INTO public.derived (id, a) VALUES (1, 1)", "UPDATE public.derived SET a = 2", as_owner=True
    )
