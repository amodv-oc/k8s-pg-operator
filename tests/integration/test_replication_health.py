"""Replication health against real servers: each state, produced on demand."""

from __future__ import annotations

import asyncio

import pytest

from pg_operator.constants import PLURAL_DATABASE
from pg_operator.errors import ReferenceNotFound
from pg_operator.reconcile.replication import observe_from_status, teardown_replication

from ..conftest import requires_replication_harness, superuser_execute
from .conftest import NS, World

pytestmark = [requires_replication_harness, pytest.mark.replication, pytest.mark.asyncio]

ALL_READY = "SELECT bool_and(srsubstate = 'r') FROM pg_subscription_rel"


def conditions(world: World) -> dict[str, dict]:
    return {c["type"]: c for c in world.status["conditions"]}


async def subscribed(world: World) -> None:
    await world.run()
    await world.eventually(ALL_READY, True, "every subscribed table to be ready")


async def test_a_healthy_replication_reports_streaming(world: World) -> None:
    await subscribed(world)
    status = await world.run()
    assert status["replicationState"] == "Streaming"
    health = status["health"]
    assert health["slotActive"] is True and health["workerRunning"] is True
    assert health["lagBytes"] >= 0 and health["retainedWalBytes"] >= 0
    assert health["walStatus"] in {"reserved", "extended"}
    assert conditions(world)["Streaming"]["status"] == "True"
    # Some tables are held back - Drifted - but nothing about the data flow is wrong.
    assert status["phase"] == "Drifted"
    assert status["subscribedTables"] == [
        {"table": "audit.events", "state": "r"},
        {"table": "public.orders", "state": "r"},
    ]


async def test_thresholds_below_the_live_numbers_make_it_lagging(world: World) -> None:
    await subscribed(world)
    world.replication["spec"]["alerting"] = {"maxRetainedWal": 1}
    status = await world.run()
    assert status["replicationState"] == "Lagging"
    assert status["phase"] == "Degraded"
    assert "retained WAL is over alerting.maxRetainedWal" in conditions(world)["Streaming"]["message"]

    world.replication["spec"]["alerting"] = {}
    assert (await world.run())["replicationState"] == "Streaming"


async def test_a_disabled_subscription_is_disabled_not_degraded(world: World) -> None:
    await subscribed(world)
    world.replication["spec"]["enabled"] = False
    status = await world.run()
    assert status["replicationState"] == "Disabled"
    assert status["phase"] != "Degraded"


async def test_a_login_that_can_no_longer_connect_stalls_it(world: World) -> None:
    """Its password changed behind the operator's back, the worker cannot reconnect.

    Teardown still clears the slot afterwards: the operator gives its own login
    a fresh password rather than trust the Secret's.
    """
    await subscribed(world)
    login = world.status["source"]["login"]
    await world.on_source(f"ALTER ROLE {login} PASSWORD 'not-the-one-in-the-secret'")
    superuser_execute(
        "SELECT pg_terminate_backend(active_pid) FROM pg_replication_slots "
        "WHERE slot_name = %s AND active_pid IS NOT NULL",
        (world.status["source"]["slot"],),
    )
    state = None
    for _ in range(30):
        state = (await world.run())["replicationState"]
        if state == "Stalled":
            break
        await asyncio.sleep(1)
    assert state == "Stalled"
    assert world.status["phase"] == "Degraded"
    assert "the apply worker is not running" in conditions(world)["Streaming"]["message"]

    await teardown_replication(world.ctx, world.replication)
    world.replication.pop("status")
    assert await world.source_slots() == []


async def test_apply_errors_stall_it_and_name_the_likely_table(world: World) -> None:
    await subscribed(world)
    # A column the source still sends is dropped on the target: apply fails.
    await world.on_target("ALTER TABLE audit.events DROP COLUMN kind", as_owner=True)
    await world.on_source("INSERT INTO audit.events VALUES (1, 'x')", as_owner=True)
    await world.eventually(
        "SELECT apply_error_count > 0 FROM pg_stat_subscription_stats", True, "an apply error"
    )
    status = await world.run()
    assert status["replicationState"] == "Stalled"
    message = conditions(world)["Streaming"]["message"]
    assert "target server's log" in message
    assert "Likely cause: audit.events (TargetColumnsMissing)" in message


async def test_an_invalidated_slot_is_slot_lost(world: World) -> None:
    """max_slot_wal_keep_size lets a lagging slot go - and the replica with it."""
    await subscribed(world)
    world.replication["spec"]["enabled"] = False
    await world.run()
    slot = world.status["source"]["slot"]
    superuser_execute("ALTER SYSTEM SET max_slot_wal_keep_size = '1MB'")
    superuser_execute("SELECT pg_reload_conf()")
    try:
        for _ in range(4):
            superuser_execute("SELECT pg_logical_emit_message(false, 'pgop', repeat('x', 2000000))")
            superuser_execute("SELECT pg_switch_wal()")
        superuser_execute("CHECKPOINT")
        status = await world.run()
        assert superuser_execute(
            "SELECT wal_status FROM pg_replication_slots WHERE slot_name = %s", (slot,)
        ) == [("lost",)]
        assert status["replicationState"] == "SlotLost"
        assert status["phase"] == "Degraded"
        assert "delete and recreate" in conditions(world)["Streaming"]["message"]
    finally:
        superuser_execute("ALTER SYSTEM RESET max_slot_wal_keep_size")
        superuser_execute("SELECT pg_reload_conf()")


async def test_a_replication_that_cannot_resolve_is_still_watched(world: World) -> None:
    """Its PostgresDB deleted under RETAIN, the pass fails - but the slot still pins WAL."""
    await subscribed(world)
    world.k8s.custom.objects.pop((PLURAL_DATABASE, NS, "orders"))
    with pytest.raises(ReferenceNotFound):
        await world.run()
    assessment = await observe_from_status(world.ctx, world.replication)
    assert assessment is not None
    assert assessment.state == "Streaming"
    assert assessment.health["slotActive"] is True
