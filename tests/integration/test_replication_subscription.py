"""The subscription: rows reaching the target, and nothing left behind by teardown.

Streams between the harness's two servers. The target server reaches the
source by container name, through ``source.connection`` - the operator's own
view of the source is a different address, as it often is in production.
"""

from __future__ import annotations

import asyncio

import pytest

from pg_operator.constants import PLURAL_REPLICATION
from pg_operator.errors import (
    ConfigurationError,
    ConnectionFailure,
    ReconcileFailure,
    ReplicationRefused,
)
from pg_operator.naming import replication_names
from pg_operator.reconcile.database import teardown_database
from pg_operator.reconcile.replication import teardown_replication

from ..conftest import emulate_rds_replication, requires_replication_harness, superuser_execute
from .conftest import NS, OPERATOR_NS, World

pytestmark = [requires_replication_harness, pytest.mark.replication, pytest.mark.asyncio]

ALL_READY = "SELECT bool_and(srsubstate = 'r') FROM pg_subscription_rel"


async def subscribed(world: World) -> None:
    """A first pass, then wait for the initial copy of every table to finish."""
    await world.run()
    await world.eventually(ALL_READY, True, "every subscribed table to be ready")


async def test_rows_stream_through_the_filter_and_column_list(world: World) -> None:
    await subscribed(world)
    status = await world.run()
    assert {c["type"]: c["status"] for c in status["conditions"]}["Ready"] == "True"
    assert status["tables"]["ready"] == status["tables"]["subscribed"] == 2

    await world.on_source(
        "INSERT INTO public.orders VALUES (1, 'AU', 10), (-5, 'NZ', 1)",
        "INSERT INTO audit.events VALUES (1, 'created'), (2, 'created')",
        as_owner=True,
    )
    await world.eventually("SELECT count(*) FROM audit.events", 2, "inserts")
    # The row filter keeps id > 0; the column list leaves total behind.
    await world.eventually("SELECT count(*) FROM public.orders", 1, "the filtered insert")
    assert await world.target_value("SELECT total FROM public.orders WHERE id = 1") is None

    await world.on_source(
        "UPDATE audit.events SET kind = 'updated' WHERE id = 1",
        "DELETE FROM audit.events WHERE id = 2",
        as_owner=True,
    )
    await world.eventually(
        "SELECT string_agg(id || '=' || kind, ',') FROM audit.events", "1=updated", "update and delete"
    )


async def test_a_converged_subscription_changes_nothing(world: World) -> None:
    await subscribed(world)
    await world.run()
    assert world.last_changes == []


async def test_a_table_added_later_arrives_with_its_rows(world: World) -> None:
    await subscribed(world)
    await world.on_source("INSERT INTO public.only_source SELECT generate_series(1, 3)", as_owner=True)
    await world.on_target("CREATE TABLE public.only_source (id int PRIMARY KEY)", as_owner=True)

    await world.run()
    assert "refreshed the subscription's tables" in world.last_changes
    await world.eventually("SELECT count(*) FROM public.only_source", 3, "the late table's initial copy")


async def test_a_table_with_rows_on_the_target_is_not_copied_into(world: World) -> None:
    """The initial copy would collide with them - so the table waits, and says why."""
    await world.on_source("INSERT INTO public.only_source VALUES (1), (2)", as_owner=True)
    await world.on_target(
        "CREATE TABLE public.only_source (id int PRIMARY KEY)",
        "INSERT INTO public.only_source VALUES (1)",
        as_owner=True,
    )
    await world.run()
    assert world.excluded()["public.only_source"] == "TargetTableNotEmpty"

    await world.on_target("TRUNCATE public.only_source", as_owner=True)
    await world.run()
    assert "public.only_source" in world.status["publishedTables"]
    await world.eventually("SELECT count(*) FROM public.only_source", 2, "the copy into the emptied table")


async def test_a_table_unpublished_and_fixed_is_not_copied_over_its_own_rows(world: World) -> None:
    """D6 takes a table out when its identity goes; its rows stay on the target."""
    await subscribed(world)
    await world.on_source("INSERT INTO audit.events VALUES (1, 'x')", as_owner=True)
    await world.eventually("SELECT count(*) FROM audit.events", 1, "the insert")

    await world.on_source("ALTER TABLE audit.events DROP CONSTRAINT events_pkey", as_owner=True)
    await world.run()
    assert world.excluded()["audit.events"] == "NoReplicaIdentity"

    await world.on_source("ALTER TABLE audit.events ADD PRIMARY KEY (id)", as_owner=True)
    await world.run()
    assert world.excluded()["audit.events"] == "TargetTableNotEmpty"


async def test_disabling_pauses_and_enabling_resumes(world: World) -> None:
    await subscribed(world)
    world.replication["spec"]["enabled"] = False
    await world.run()
    assert await world.target_value(
        "SELECT bool_and(subenabled) FROM pg_subscription "
        "WHERE subdbid = (SELECT oid FROM pg_database WHERE datname = current_database())"
    ) is False

    await world.on_source("INSERT INTO audit.events VALUES (9, 'while disabled')", as_owner=True)
    world.replication["spec"]["enabled"] = True
    await world.run()
    assert "enabled the subscription" in world.last_changes
    await world.eventually("SELECT count(*) FROM audit.events", 1, "the change made while disabled")


async def test_a_regenerated_password_reaches_the_subscription(world: World) -> None:
    await subscribed(world)
    secret = world.status["credentialsSecret"]["name"]
    await world.k8s.core.delete_namespaced_secret(name=secret, namespace=OPERATOR_NS)

    await world.run()
    assert "updated the subscription's connection" in world.last_changes
    await world.on_source("INSERT INTO audit.events VALUES (1, 'after')", as_owner=True)
    await world.eventually("SELECT count(*) FROM audit.events", 1, "a change after the new password")


async def test_teardown_leaves_no_slot_and_keeps_the_rows(world: World) -> None:
    await subscribed(world)
    await world.on_source("INSERT INTO audit.events VALUES (1, 'kept')", as_owner=True)
    await world.eventually("SELECT count(*) FROM audit.events", 1, "the insert")
    subscription = world.status["target"]["subscription"]

    message = await teardown_replication(world.ctx, world.replication)
    world.replication.pop("status")
    assert f"subscription {subscription}" in message
    assert await world.source_slots() == []
    assert await world.target_value("SELECT count(*) FROM pg_subscription") == 0
    assert await world.target_value("SELECT count(*) FROM audit.events") == 1


async def test_an_unreachable_target_does_not_keep_the_slot_alive(world: World) -> None:
    """The source is cleared first when the target cannot be reached: the slot is the danger."""
    await subscribed(world)
    port = world.move_instance("dst", 1)
    with pytest.raises(ConnectionFailure, match="cannot reach target"):
        await teardown_replication(world.ctx, world.replication)
    assert await world.source_slots() == []

    world.move_instance("dst", port)
    message = await teardown_replication(world.ctx, world.replication)
    world.replication.pop("status")
    assert "(detached from its slot)" in message
    assert await world.target_value("SELECT count(*) FROM pg_subscription") == 0
    assert await world.source_slots() == []


async def test_an_unreachable_source_holds_the_finalizer_until_it_is_cleared(world: World) -> None:
    await subscribed(world)
    port = world.move_instance("src", 1)
    with pytest.raises(ConnectionFailure, match="cannot reach source"):
        await teardown_replication(world.ctx, world.replication)
    # The target could still reach the source, so its DROP SUBSCRIPTION took the slot.
    assert await world.target_value("SELECT count(*) FROM pg_subscription") == 0

    world.move_instance("src", port)
    await teardown_replication(world.ctx, world.replication)
    world.replication.pop("status")
    assert await world.source_slots() == []


async def test_an_idle_slot_left_behind_is_adopted(world: World) -> None:
    """A slot named for this replication, with no subscription, is its own leftover.

    Its position predates the subscription's initial copy, which is all apply
    needs, so it is used rather than dropped and recreated.
    """
    slot = replication_names(NS, world.replication["metadata"]["name"]).subscription
    superuser_execute(
        "SELECT pg_create_logical_replication_slot(%s, 'pgoutput')", (slot,), database=world.source_db
    )
    await world.run()
    assert f"adopted idle slot {slot}" in world.last_changes
    assert world.status["created"]["subscription"] is True
    await world.on_source("INSERT INTO audit.events VALUES (1, 'through the adopted slot')", as_owner=True)
    await world.eventually("SELECT count(*) FROM audit.events", 1, "a change through the adopted slot")


async def test_opposite_subscriptions_created_at_once_do_not_deadlock(world: World) -> None:
    """Creating a slot waits for the source's running transactions.

    Were the slot made by CREATE SUBSCRIPTION, inside its transaction on the
    target, two replications in opposite directions between the same servers
    would each wait on the other's until the statement timeout - and retry into
    the same deadlock. The slot is made from the source instead.
    """
    other = await world.reverse_pair("rev")
    emulate_first = [world.run(emulate=False), other.run(emulate=False)]
    results = await asyncio.gather(*emulate_first, return_exceptions=True)
    # A brand-new login cannot stream on the harness until rds_replication is emulated.
    assert all(isinstance(r, ReplicationRefused) for r in results), results
    emulate_rds_replication(target=True)
    await asyncio.gather(world.run(emulate=False), other.run(emulate=False))
    assert world.status["created"]["subscription"] and other.status["created"]["subscription"]


async def test_a_foreign_subscription_is_not_taken_over(world: World) -> None:
    taken = replication_names(NS, world.replication["metadata"]["name"]).subscription
    await world.on_target(
        # A non-superuser's subscription must carry a password, even unconnected.
        f"CREATE SUBSCRIPTION {taken} CONNECTION 'host=nowhere dbname=x password=x' "
        "PUBLICATION p WITH (connect = false)"
    )
    with pytest.raises(ConfigurationError, match="will not take it over"):
        await world.run()
    assert "status" not in world.replication
    async with world.pools.connection(world.source, world.source_db) as conn:
        from psycopg import sql

        from pg_operator.postgres.sql import fetch_scalar

        assert await fetch_scalar(conn, sql.SQL("SELECT count(*) FROM pg_publication")) == 0


async def test_a_database_a_replication_uses_is_not_dropped_from_under_it(world: World) -> None:
    """D7: on the source FORCE would kill the slot; on the target DROP DATABASE would fail."""
    await subscribed(world)
    for name in ("orders", "orders-copy"):
        database = world.k8s.custom.get_stored("postgresdbs", NS, name)
        with pytest.raises(ReconcileFailure, match=f"{world.replication['metadata']['name']}"):
            await teardown_database(world.ctx, database)

    await teardown_replication(world.ctx, world.replication)
    world.replication.pop("status")
    world.k8s.custom.objects.pop((PLURAL_REPLICATION, NS, world.replication["metadata"]["name"]))
    await world.ctx.pools.close_database("src", world.source_db)
    message = await teardown_database(world.ctx, world.k8s.custom.get_stored("postgresdbs", NS, "orders"))
    assert f"dropped database {world.source_db}" in message
