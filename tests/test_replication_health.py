"""Replication health: every state, their precedence, and what reaches status and Prometheus."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from prometheus_client import REGISTRY

from pg_operator import status as st
from pg_operator.models import AlertingSpec
from pg_operator.postgres.subscription import SlotHealth, SubscriptionHealth
from pg_operator.reconcile.health import Observation, assess
from pg_operator.units import format_bytes

NOW = datetime(2026, 10, 21, 12, 0, 0, tzinfo=UTC)
LIMITS = AlertingSpec.model_validate({"maxLag": 1000, "maxRetainedWal": 10_000, "maxSlotInactive": "10m"})

SLOT = SlotHealth(active=True, wal_status="reserved", safe_wal_size=None, lag_bytes=10, retained_bytes=100)
SUBSCRIPTION = SubscriptionHealth(
    worker_running=True, last_message_at="2026-10-21T11:59:58Z",
    apply_error_count=0, sync_error_count=0, conflicts={},
)


def judge(
    slot: SlotHealth | None = SLOT,
    subscription: SubscriptionHealth | None = SUBSCRIPTION,
    tables: dict[str, str] | None = None,
    **options: Any,
):
    settings: dict[str, Any] = {
        "previous": {}, "previous_state": None, "enabled": True,
        "alerting": LIMITS, "suspects": [], "now": NOW,
    }
    settings.update(options)
    return assess(Observation(slot, subscription, tables or {"public.a": "r"}), **settings)


def test_a_healthy_replication_is_streaming() -> None:
    result = judge()
    assert (result.state, result.streaming) == ("Streaming", True)
    assert result.health["lag"] == "10 B" and result.health["retainedWalBytes"] == 100
    assert result.health["observedAt"] == "2026-10-21T12:00:00Z"


def test_a_table_still_copying_is_initializing_and_healthy() -> None:
    result = judge(tables={"public.a": "r", "public.b": "d"})
    assert (result.state, result.streaming) == ("Initializing", True)
    assert "1 of 2 table(s)" in result.message


def test_over_either_threshold_is_lagging() -> None:
    assert judge(replace(SLOT, lag_bytes=1001)).state == "Lagging"
    over = judge(replace(SLOT, retained_bytes=10_001))
    assert (over.state, over.streaming) == ("Lagging", False)
    assert "over alerting.maxRetainedWal" in over.message


def test_lagging_clears_only_below_four_fifths_of_the_threshold() -> None:
    hovering = replace(SLOT, retained_bytes=9_000)
    assert judge(hovering, previous_state="Lagging").state == "Lagging"
    assert judge(hovering, previous_state="Streaming").state == "Streaming"
    assert judge(replace(SLOT, retained_bytes=7_900), previous_state="Lagging").state == "Streaming"


def test_a_stopped_apply_worker_is_a_stall() -> None:
    result = judge(subscription=replace(SUBSCRIPTION, worker_running=False))
    assert (result.state, result.streaming) == ("Stalled", False)
    assert "apply worker is not running" in result.message
    assert "target server's log" in result.message


def test_rising_error_counts_are_a_stall_and_a_reset_is_not() -> None:
    errors = replace(SUBSCRIPTION, apply_error_count=5)
    rose = judge(subscription=errors, previous={"applyErrorCount": 2})
    assert rose.state == "Stalled" and "apply errors rose from 2 to 5" in rose.message
    assert judge(subscription=errors, previous={"applyErrorCount": 5}).state == "Streaming"
    assert judge(subscription=SUBSCRIPTION, previous={"applyErrorCount": 9}).state == "Streaming"


def test_suspects_are_named_in_a_stall() -> None:
    result = judge(
        subscription=replace(SUBSCRIPTION, worker_running=False),
        suspects=["audit.events (TargetColumnsMissing)"],
    )
    assert "Likely cause: audit.events (TargetColumnsMissing)" in result.message


def test_a_slot_without_a_consumer_stalls_only_after_max_slot_inactive() -> None:
    idle = replace(SLOT, active=False)
    fresh = judge(idle)
    assert fresh.state == "Streaming" and fresh.health["inactiveSince"] == "2026-10-21T12:00:00Z"
    since = (NOW - timedelta(minutes=11)).strftime("%Y-%m-%dT%H:%M:%SZ")
    stale = judge(idle, previous={"inactiveSince": since})
    assert stale.state == "Stalled" and f"no consumer since {since}" in stale.message
    # Once a consumer is back, the clock resets.
    assert judge(SLOT, previous={"inactiveSince": since}).health["inactiveSince"] is None


def test_a_lost_slot_outranks_everything_including_disabled() -> None:
    lost = replace(SLOT, wal_status="lost", retained_bytes=None)
    for enabled in (True, False):
        result = judge(lost, enabled=enabled)
        assert (result.state, result.streaming) == ("SlotLost", False)
        assert "max_slot_wal_keep_size" in result.message
    gone = judge(None)
    assert gone.state == "SlotLost" and "no longer exists" in gone.message


def test_disabled_is_its_own_state() -> None:
    result = judge(enabled=False, subscription=replace(SUBSCRIPTION, worker_running=False))
    assert (result.state, result.streaming) == ("Disabled", False)


def test_what_could_not_be_read_is_left_out() -> None:
    result = judge(subscription=None)
    assert result.health["workerRunning"] is None and result.health["applyErrorCount"] is None
    assert result.health["lagBytes"] == 10


# --- phase -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("state", "status", "phase"),
    [
        ("Streaming", "True", "Ready"),
        ("Initializing", "True", "Ready"),
        ("Disabled", "False", "Ready"),
        ("Lagging", "False", "Degraded"),
        ("Stalled", "False", "Degraded"),
        ("SlotLost", "False", "Degraded"),
    ],
)
def test_only_unhealthy_states_degrade_the_phase(state: str, status: str, phase: str) -> None:
    conditions = [
        st.ready("ok", 1),
        st.condition("Streaming", status, state, "", observed_generation=1),  # type: ignore[arg-type]
    ]
    assert st.phase_from_conditions(conditions) == phase


def test_degraded_outranks_drifted() -> None:
    conditions = [
        st.ready("ok", 1),
        st.drifted("a table is held back", 1, reason="TargetTableMissing"),
        st.condition("Streaming", "False", "Stalled", "", observed_generation=1),
    ]
    assert st.phase_from_conditions(conditions) == "Degraded"


# --- metrics -----------------------------------------------------------------------


def sample(metric: str, /, **labels: str) -> float | None:
    return REGISTRY.get_sample_value(metric, labels)


def test_metrics_follow_the_observation_and_go_with_the_resource() -> None:
    from pg_operator.handlers.replication import forget_metrics, publish_metrics

    result = judge(replace(SLOT, retained_bytes=10_001))
    publish_metrics("team-a", "m", result, {"published": 3, "ready": 2})
    labels = {"namespace": "team-a", "name": "m"}
    assert sample("pg_operator_replication_retained_wal_bytes", **labels) == 10_001
    assert sample("pg_operator_replication_state", **labels, state="Lagging") == 1
    assert sample("pg_operator_replication_state", **labels, state="Streaming") == 0
    assert sample("pg_operator_replication_tables", **labels, state="ready") == 2
    assert sample("pg_operator_replication_errors", **labels, kind="apply") == 0

    forget_metrics("team-a", "m")
    assert sample("pg_operator_replication_retained_wal_bytes", **labels) is None
    assert sample("pg_operator_replication_state", **labels, state="Lagging") is None
    assert sample("pg_operator_replication_tables", **labels, state="ready") is None


@pytest.mark.parametrize(
    ("value", "text"),
    [(0, "0 B"), (1023, "1023 B"), (1024, "1 KiB"), (12_288, "12 KiB"), (1536 * 1024**2, "1.5 GiB")],
)
def test_bytes_read_the_way_people_write_them(value: int, text: str) -> None:
    assert format_bytes(value) == text
