"""The subscription: rows reaching the target, and nothing left behind by teardown.

Streams between the harness's two servers. The target server reaches the
source by container name, through ``source.connection`` - the operator's own
view of the source is a different address, as it often is in production.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest
from psycopg import sql

from pg_operator.constants import PLURAL_INSTANCE, PLURAL_REPLICATION
from pg_operator.errors import (
    ConfigurationError,
    ConnectionFailure,
    ReconcileFailure,
    ReplicationRefused,
    SourceUnreachableFromTarget,
)
from pg_operator.naming import replication_names
from pg_operator.postgres import subscription as sub
from pg_operator.postgres.sql import fetch_scalar
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


@pytest.mark.parametrize("side", ["dst", "src"])
async def test_a_version_floor_does_not_stop_teardown(world: World, side: str) -> None:
    """minServerVersion is a condition for creating objects on a server, not for removing them."""
    await subscribed(world)
    status = world.status
    world.k8s.custom.get_stored(PLURAL_INSTANCE, "", side)["spec"]["minServerVersion"] = 99
    world.ctx.invalidate(side)

    message = await teardown_replication(world.ctx, world.replication)
    world.replication.pop("status")
    for name in (status["target"]["subscription"], status["source"]["publication"], status["source"]["login"]):
        assert name in message
    assert await world.source_slots() == []
    assert await world.target_value("SELECT count(*) FROM pg_subscription") == 0
    assert world.k8s.core.get(OPERATOR_NS, status["credentialsSecret"]["name"]) is None


async def make_unusable(world: World, instance: str, problem: str) -> Callable[[], None]:
    """Break a PostgresInstance that still exists; returns what puts it right."""
    stored = world.k8s.custom.get_stored(PLURAL_INSTANCE, "", instance)
    spec = dict(stored["spec"])
    endpoint = world.target if instance == "dst" else world.source
    if problem == "an invalid spec":
        stored["spec"]["sslMode"] = "verify-full"  # without the CA bundle it requires
    else:
        await world.k8s.core.delete_namespaced_secret(name=f"{instance}-superuser", namespace=OPERATOR_NS)
    world.ctx.invalidate(instance)

    def restore() -> None:
        stored["spec"] = spec
        world.k8s.core.seed(
            OPERATOR_NS, f"{instance}-superuser", {"username": endpoint.username, "password": endpoint.password}
        )
        world.ctx.invalidate(instance)

    return restore


async def login_exists(world: World, login: str) -> bool:
    async with world.pools.connection(world.source) as conn:
        return bool(
            await fetch_scalar(conn, sql.SQL("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = %s)"), (login,))
        )


@pytest.mark.parametrize("problem", ["an invalid spec", "no credentials Secret"])
async def test_an_unusable_target_instance_holds_the_finalizer_but_not_the_slot(
    world: World, problem: str
) -> None:
    """A target instance that exists but cannot be used may still hold the subscription.

    So the finalizer stays, with a temporary error naming why - and the source,
    whose slot is the part that does damage, is cleared regardless.
    """
    await subscribed(world)
    status = world.status
    restore = await make_unusable(world, "dst", problem)
    with pytest.raises(ReconcileFailure, match="cannot clear target dst") as raised:
        await teardown_replication(world.ctx, world.replication)
    # Anything run_teardown takes for "nothing can ever be cleaned up" would release the finalizer.
    assert not isinstance(raised.value, ConfigurationError)
    assert await world.source_slots() == []
    assert not await login_exists(world, status["source"]["login"])
    assert await world.target_value("SELECT count(*) FROM pg_subscription") == 1

    restore()
    message = await teardown_replication(world.ctx, world.replication)
    world.replication.pop("status")
    assert "(detached from its slot)" in message
    assert await world.target_value("SELECT count(*) FROM pg_subscription") == 0


async def test_an_unusable_source_instance_holds_the_finalizer(world: World) -> None:
    await subscribed(world)
    status = world.status
    restore = await make_unusable(world, "src", "an invalid spec")
    with pytest.raises(ReconcileFailure, match="cannot clear source src"):
        await teardown_replication(world.ctx, world.replication)
    # The target could still reach the source, so its DROP SUBSCRIPTION took the slot.
    assert await world.target_value("SELECT count(*) FROM pg_subscription") == 0
    # The login is still there, and so is its credential.
    assert await login_exists(world, status["source"]["login"])
    assert world.k8s.core.get(OPERATOR_NS, status["credentialsSecret"]["name"]) is not None

    restore()
    await teardown_replication(world.ctx, world.replication)
    world.replication.pop("status")
    assert await world.source_slots() == []
    assert not await login_exists(world, status["source"]["login"])
    assert world.k8s.core.get(OPERATOR_NS, status["credentialsSecret"]["name"]) is None


# --- table-sync slots: named for the subscription's oid and the target server ---
#
# pg_<subscription oid>_sync_<relid>_<system identifier>, the oid and the
# identifier both the target's. Another target subscribing to the same source
# can hold a subscription with the same oid, so only the identifier tells
# their slots apart. A slot made by hand stands in for each: the harness cannot
# make two servers agree on an oid.


async def target_system_identifier(world: World) -> int:
    value = await world.target_value("SELECT system_identifier FROM pg_control_system()")
    return int(value) % 2**64


def create_slot(world: World, name: str) -> None:
    superuser_execute(
        "SELECT pg_create_logical_replication_slot(%s, 'pgoutput')", (name,), database=world.source_db
    )


def drop_slot_if_left(name: str) -> None:
    superuser_execute(
        "SELECT pg_drop_replication_slot(slot_name) FROM pg_replication_slots WHERE slot_name = %s", (name,)
    )


async def test_teardown_drops_its_own_table_sync_slots_and_not_another_targets(world: World) -> None:
    await subscribed(world)
    status = world.status
    oid = status["target"]["subscriptionOid"]
    subscriber = await target_system_identifier(world)
    # What a detached DROP SUBSCRIPTION leaves behind, and another target's slot mid-copy.
    ours = f"pg_{oid}_sync_16384_{subscriber}"
    stranger = f"pg_{oid}_sync_16384_{(subscriber + 1) % 2**64}"
    # Named exactly as this subscription's would be, but in another database:
    # a table-sync worker's slot is always in the database it streams from, so
    # this is someone else's - as it would be where two targets share a system
    # identifier as well as an oid.
    elsewhere = f"pg_{oid}_sync_16385_{subscriber}"
    create_slot(world, ours)
    create_slot(world, stranger)
    superuser_execute("SELECT pg_create_logical_replication_slot(%s, 'pgoutput')", (elsewhere,))
    try:
        await teardown_replication(world.ctx, world.replication)
        world.replication.pop("status")
        assert await world.source_slots() == sorted([stranger, elsewhere])
    finally:
        drop_slot_if_left(ours)
        drop_slot_if_left(stranger)
        drop_slot_if_left(elsewhere)
    assert status["target"]["systemIdentifier"] == str(subscriber)


async def test_without_the_targets_identifier_a_table_sync_slot_is_left_and_named(world: World) -> None:
    """Neither guess is safe: dropping it may break another target's copy, leaving it pins WAL.

    So it is left alone, and the finalizer held with an error that names it,
    until it goes - by its own subscriber finishing, or by hand.
    """
    await subscribed(world)
    status = world.status
    world.status["target"].pop("systemIdentifier", None)
    unknown = f"pg_{status['target']['subscriptionOid']}_sync_16384_1234567890"
    create_slot(world, unknown)
    try:
        with pytest.raises(ReconcileFailure, match=unknown):
            await teardown_replication(world.ctx, world.replication)
        assert await world.source_slots() == [unknown]
        assert await world.target_value("SELECT count(*) FROM pg_subscription") == 0
    finally:
        drop_slot_if_left(unknown)

    await teardown_replication(world.ctx, world.replication)
    world.replication.pop("status")
    assert await world.source_slots() == []
    assert not await login_exists(world, status["source"]["login"])


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


async def test_a_subscription_never_exists_without_its_marker(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CREATE SUBSCRIPTION and its COMMENT commit together, or neither does.

    An unmarked subscription of this name is refused as someone else's, so one
    left by a failure between the two would fail every later pass, and its
    deletion could never finish while it kept re-taking its slot.
    """
    real = sub.execute

    async def failing(conn, query, params=None, *, describe=None):  # type: ignore[no-untyped-def]
        if "COMMENT ON SUBSCRIPTION" in query.as_string(conn):
            raise ReconcileFailure("server closed the connection unexpectedly")
        return await real(conn, query, params, describe=describe)

    monkeypatch.setattr(sub, "execute", failing)
    with pytest.raises(ReconcileFailure, match="closed the connection"):
        await world.run()
    monkeypatch.undo()
    assert await world.target_value("SELECT count(*) FROM pg_subscription") == 0

    await subscribed(world)
    await world.on_source("INSERT INTO audit.events VALUES (1, 'after the retry')", as_owner=True)
    await world.eventually("SELECT count(*) FROM audit.events", 1, "a change through the retried subscription")
    await teardown_replication(world.ctx, world.replication)
    world.replication.pop("status")
    assert await world.source_slots() == []
    assert await world.target_value("SELECT count(*) FROM pg_subscription") == 0


async def test_a_slot_made_by_a_pass_that_stopped_before_recording_it_is_torn_down(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The slot is recorded before it is made: one teardown cannot find pins WAL until the disk fills."""
    real = sub.create_slot

    async def made_then_stopped(conn, name):  # type: ignore[no-untyped-def]
        await real(conn, name)
        raise ReconcileFailure("the operator was stopped")

    monkeypatch.setattr(sub, "create_slot", made_then_stopped)
    with pytest.raises(ReconcileFailure, match="stopped"):
        await world.run()
    monkeypatch.undo()
    slot = world.status["source"]["slot"]
    assert await world.source_slots() == [slot]
    assert world.status["created"]["slot"] is True

    await teardown_replication(world.ctx, world.replication)
    world.replication.pop("status")
    assert await world.source_slots() == []


async def test_its_own_idle_slot_needs_no_free_slot(world: World) -> None:
    """A slot an earlier pass made is adopted, however full the source's slots are otherwise."""
    good_host = world.replication["spec"]["source"]["connection"]["host"]
    world.replication["spec"]["source"]["connection"]["host"] = "no-such-host.invalid"
    with pytest.raises(SourceUnreachableFromTarget):
        await world.run()
    slot = world.status["source"]["slot"]
    assert world.status["created"]["slot"] is True
    assert await world.source_slots() == [slot]

    limit = int(superuser_execute("SHOW max_replication_slots")[0][0])
    used = int(superuser_execute("SELECT count(*) FROM pg_replication_slots")[0][0])
    fillers = [f"rs_filler_{world.unique}_{i}" for i in range(limit - used)]
    try:
        for filler in fillers:
            superuser_execute("SELECT pg_create_physical_replication_slot(%s)", (filler,))
        world.replication["spec"]["source"]["connection"]["host"] = good_host
        await world.run()
        assert f"adopted idle slot {slot}" in world.last_changes
        assert f"created subscription {slot}" in world.last_changes
    finally:
        for filler in fillers:
            superuser_execute("SELECT pg_drop_replication_slot(%s)", (filler,))


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


async def test_a_reverse_replication_does_not_stop_the_streaming_one(world: World) -> None:
    """The newcomer closes the loop, so it alone is refused - from whichever namespace.

    The streaming replication keeps converging: here, D6's unpublish of a
    table that has lost its replica identity, without which the source's
    UPDATEs to it fail.
    """
    import re

    from pg_operator.reconcile.replication import reconcile_replication

    await subscribed(world)
    reverse = {
        "apiVersion": "postgres.ourcommunity.com.au/v1alpha1",
        "kind": "PostgresReplication",
        "metadata": {
            "name": "newcomer",
            "namespace": "team-b",
            "generation": 1,
            "creationTimestamp": "2099-01-01T00:00:00Z",
        },
        "spec": {
            "source": {"dbRef": {"name": "orders-copy", "namespace": NS}},
            "target": {"dbRef": {"name": "orders", "namespace": NS}},
            "schemas": [{"name": "audit"}],
        },
    }
    world.k8s.custom.add(reverse, PLURAL_REPLICATION)
    streaming = f"{NS}/{world.replication['metadata']['name']}"
    with pytest.raises(ConfigurationError, match=f"cycle with {re.escape(streaming)}"):
        await reconcile_replication(world.ctx, reverse)

    await world.on_source("ALTER TABLE audit.events DROP CONSTRAINT events_pkey", as_owner=True)
    status = await world.run()
    assert world.excluded()["audit.events"] == "NoReplicaIdentity"
    assert "audit.events" not in await world.publication()
    assert {c["type"]: c["status"] for c in status["conditions"]}["Ready"] == "True"
    await world.on_source(
        "INSERT INTO audit.events VALUES (1, 'x')",
        "UPDATE audit.events SET kind = 'y'",
        "INSERT INTO public.orders VALUES (7, 'AU', 1)",
        as_owner=True,
    )
    await world.eventually("SELECT count(*) FROM public.orders", 1, "a change made after the reverse")


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


# --- a published table the subscription no longer carries ----------------------
#
# Dropping a table on the target takes it out of the subscription, while the
# publication still lists it. Whatever then has its name is new to the
# subscription, and a refresh would subscribe it - with an initial copy.


def warned(world: World) -> set[tuple[str, str]]:
    return {(entry["table"], entry["reason"]) for entry in world.status["tableWarnings"]}


async def test_a_table_rewritten_on_the_target_is_not_copied_over_its_rows(world: World) -> None:
    """CREATE TABLE AS, drop, rename: the same rows in a relation the subscription never had."""
    await subscribed(world)
    await world.on_source("INSERT INTO audit.events SELECT g, 'x' FROM generate_series(1, 5) g", as_owner=True)
    await world.eventually("SELECT count(*) FROM audit.events", 5, "the inserts")

    await world.on_target(
        "CREATE TABLE audit.events_new AS SELECT * FROM audit.events",
        "DROP TABLE audit.events",
        "ALTER TABLE audit.events_new RENAME TO events",
        "ALTER TABLE audit.events ADD PRIMARY KEY (id)",
        as_owner=True,
    )
    await world.run()

    assert world.excluded()["audit.events"] == "TargetTableNotEmpty"
    assert "audit.events" not in await world.publication()
    assert "removed audit.events from the publication" in world.last_changes
    assert "refreshed the subscription's tables" not in world.last_changes
    # Held back, it stays held back - and nothing was copied in beside its rows.
    await world.run()
    assert world.last_changes == []
    assert await world.target_value("SELECT count(*) FROM audit.events") == 5
    assert await world.target_value("SELECT sync_error_count FROM pg_stat_subscription_stats") == 0


async def test_a_table_dropped_on_the_target_is_unpublished_and_comes_back_with_a_copy(
    world: World,
) -> None:
    await subscribed(world)
    await world.on_source("INSERT INTO audit.events VALUES (1, 'before')", as_owner=True)
    await world.eventually("SELECT count(*) FROM audit.events", 1, "the insert")

    await world.on_target("DROP TABLE audit.events", as_owner=True)
    # REFRESH PUBLICATION fails on a published table the target lacks, so the
    # pass must not leave it published and refresh anyway.
    status = await world.run()
    assert world.excluded()["audit.events"] == "TargetTableMissing"
    assert "audit.events" not in await world.publication()
    assert {c["type"]: c["status"] for c in status["conditions"]}["Ready"] == "True"

    await world.on_source("INSERT INTO audit.events VALUES (2, 'meanwhile')", as_owner=True)
    await world.on_target("CREATE TABLE audit.events (id int PRIMARY KEY, kind text)", as_owner=True)
    await world.run()
    assert "added audit.events" in world.last_changes
    await world.eventually("SELECT count(*) FROM audit.events", 2, "the table's fresh copy")


async def test_a_partition_the_target_lacks_holds_the_refresh_back_not_the_table(world: World) -> None:
    """A partitioned table whose other partitions are streaming stays published.

    Unpublishing it would stop them all, and coming back would need their
    target emptied. Refreshing would fail on the missing partition - so the
    refresh waits for the target instead, and the pass still completes.
    """
    root = "CREATE TABLE public.readings (id int, at date, PRIMARY KEY (id, at)) PARTITION BY RANGE (at)"
    y2026 = (
        "CREATE TABLE public.readings_2026 PARTITION OF public.readings "
        "FOR VALUES FROM ('2026-01-01') TO ('2027-01-01')"
    )
    y2027 = (
        "CREATE TABLE public.readings_2027 PARTITION OF public.readings "
        "FOR VALUES FROM ('2027-01-01') TO ('2028-01-01')"
    )
    await world.on_source(root, y2026, as_owner=True)
    await world.on_target(root, y2026, as_owner=True)
    await subscribed(world)
    await world.on_source("INSERT INTO public.readings VALUES (1, '2026-06-01')", as_owner=True)
    await world.eventually("SELECT count(*) FROM public.readings", 1, "the 2026 row")

    # The source gains a partition first.
    await world.on_source(y2027, as_owner=True)
    await world.run()
    assert {("public.readings", "TargetTableMissing"), ("public.readings", "RefreshHeldBack")} <= warned(world)
    assert "public.readings" in await world.publication()
    assert "refreshed the subscription's tables" not in world.last_changes
    await world.run()
    assert world.last_changes == []
    # The partitions the subscription carries are untouched.
    await world.on_source("INSERT INTO public.readings VALUES (2, '2026-07-01')", as_owner=True)
    await world.eventually("SELECT count(*) FROM public.readings", 2, "a later 2026 row")

    # Once the target has it, the refresh goes ahead and the partition is copied.
    await world.on_source("INSERT INTO public.readings VALUES (3, '2027-02-01')", as_owner=True)
    await world.on_target(y2027, as_owner=True)
    await world.run()
    assert "refreshed the subscription's tables" in world.last_changes
    assert not {reason for table, reason in warned(world) if table == "public.readings"}
    await world.eventually("SELECT count(*) FROM public.readings_2027", 1, "the new partition's row")
