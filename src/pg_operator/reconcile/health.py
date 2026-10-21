"""Replication health: what the two servers report, and the state that puts a replication in.

Pure - observations in, an assessment out - so every state and its precedence
is tested without a server. The numbers go to ``status.health`` and to
Prometheus; the condition carries only the state and a message without live
numbers, so it changes on a transition rather than on every pass (design D4).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from ..models import AlertingSpec
from ..postgres.subscription import SlotHealth, SubscriptionHealth
from ..units import format_bytes

State = Literal["Initializing", "Streaming", "Lagging", "Stalled", "SlotLost", "Disabled"]

#: A Lagging replication recovers only once below this share of its
#: threshold, so lag hovering at the line does not flap the state.
RECOVERY_FRACTION = 0.8

_ERROR_TEXT = (
    "PostgreSQL counts apply errors but does not expose them; the error itself is in the "
    "target server's log (CloudWatch on RDS)"
)


@dataclass(frozen=True, slots=True)
class Observation:
    slot: SlotHealth | None
    subscription: SubscriptionHealth | None
    #: ``schema.table -> srsubstate`` for every subscribed table.
    tables: dict[str, str]


@dataclass(frozen=True, slots=True)
class Assessment:
    state: State
    #: The Streaming condition: True for Streaming and Initializing.
    streaming: bool
    message: str
    health: dict[str, Any]


def assess(
    observation: Observation,
    *,
    previous: dict[str, Any],
    previous_state: str | None,
    enabled: bool,
    alerting: AlertingSpec,
    suspects: list[str],
    now: datetime | None = None,
) -> Assessment:
    """Decide the state. Worst first: a lost slot outranks being switched off."""
    now = now or datetime.now(UTC)
    slot, subscription = observation.slot, observation.subscription
    health = _health(observation, previous, now)

    if slot is None or slot.wal_status == "lost":
        why = (
            "the slot no longer exists on the source"
            if slot is None
            else "max_slot_wal_keep_size invalidated the slot (wal_status lost)"
        )
        return Assessment(
            "SlotLost",
            False,
            f"{why}; changes since are gone, so the replica can only be rebuilt - delete and "
            "recreate this PostgresReplication to copy the tables afresh",
            health,
        )

    if not enabled:
        return Assessment(
            "Disabled",
            False,
            "spec.enabled is false; the slot keeps retaining WAL on the source meanwhile",
            health,
        )

    stalls: list[str] = []
    if subscription is not None and not subscription.worker_running:
        stalls.append("the apply worker is not running")
    inactive_since = health.get("inactiveSince")
    if inactive_since and _seconds_since(inactive_since, now) > alerting.max_slot_inactive_seconds:
        stalls.append(f"the slot has had no consumer since {inactive_since}")
    for kind, key in (("apply", "applyErrorCount"), ("table-sync", "syncErrorCount")):
        before, after = previous.get(key), health.get(key)
        if isinstance(before, int) and isinstance(after, int) and after > before:
            stalls.append(f"{kind} errors rose from {before} to {after}")
    if stalls:
        message = "; ".join(stalls) + f". {_ERROR_TEXT}"
        if suspects:
            message += f". Likely cause: {'; '.join(suspects)}"
        return Assessment("Stalled", False, message, health)

    fraction = RECOVERY_FRACTION if previous_state == "Lagging" else 1.0
    over = [
        f"{label} is over alerting.{field}"
        for label, value, limit, field in (
            ("lag", slot.lag_bytes, alerting.max_lag_bytes, "maxLag"),
            ("retained WAL", slot.retained_bytes, alerting.max_retained_wal_bytes, "maxRetainedWal"),
        )
        if value is not None and value > limit * fraction
    ]
    if over:
        return Assessment("Lagging", False, "; ".join(over), health)

    copying = sorted(table for table, state in observation.tables.items() if state != "r")
    if copying:
        return Assessment(
            "Initializing",
            True,
            f"{len(copying)} of {len(observation.tables)} table(s) still in their initial copy",
            health,
        )
    return Assessment("Streaming", True, "changes are flowing within the alerting thresholds", health)


def _health(observation: Observation, previous: dict[str, Any], now: datetime) -> dict[str, Any]:
    """``status.health``. A value that could not be read is None, which removes it from status."""
    slot, subscription = observation.slot, observation.subscription
    inactive = slot is not None and not slot.active
    return {
        "observedAt": _iso(now),
        "slotActive": slot.active if slot else None,
        "walStatus": slot.wal_status if slot else None,
        "safeWalSizeBytes": slot.safe_wal_size if slot else None,
        "lagBytes": slot.lag_bytes if slot else None,
        "lag": format_bytes(slot.lag_bytes) if slot and slot.lag_bytes is not None else None,
        "retainedWalBytes": slot.retained_bytes if slot else None,
        "retainedWal": (
            format_bytes(slot.retained_bytes) if slot and slot.retained_bytes is not None else None
        ),
        # Tracked here because pg_replication_slots.inactive_since only exists from 17.
        "inactiveSince": (previous.get("inactiveSince") or _iso(now)) if inactive else None,
        "workerRunning": subscription.worker_running if subscription else None,
        "lastMessageAt": subscription.last_message_at if subscription else None,
        "applyErrorCount": subscription.apply_error_count if subscription else None,
        "syncErrorCount": subscription.sync_error_count if subscription else None,
        "conflicts": (subscription.conflicts or None) if subscription else None,
    }


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _seconds_since(stamp: str, now: datetime) -> float:
    then = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    return (now - then).total_seconds()
