"""Kubernetes-style status conditions.

Conditions follow ``metav1.Condition``: a stable ``type``, ``True``/``False``/
``Unknown`` status, a CamelCase ``reason``, a human ``message``, and
``observedGeneration`` so a stale condition is recognisable. ``lastTransitionTime``
is only refreshed when the status actually flips, which keeps a steady-state
reconcile from rewriting the object.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from .constants import COND_DRIFTED, COND_REACHABLE, COND_READY, COND_SYNCED

ConditionStatus = Literal["True", "False", "Unknown"]

#: Order conditions are rendered in, so kubectl output stays predictable.
_CONDITION_ORDER = (COND_READY, COND_REACHABLE, COND_SYNCED, COND_DRIFTED)


def now() -> str:
    """RFC 3339 timestamp in UTC, as the API server would write it."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def condition(
    type_: str,
    status: ConditionStatus,
    reason: str,
    message: str = "",
    *,
    observed_generation: int | None = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "type": type_,
        "status": status,
        "reason": reason,
        "message": message[:32_000],
        "lastTransitionTime": now(),
    }
    if observed_generation is not None:
        entry["observedGeneration"] = observed_generation
    return entry


def merge_conditions(
    existing: list[dict[str, Any]] | None, updates: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Merge new conditions over existing ones, preserving transition times."""
    by_type: dict[str, dict[str, Any]] = {
        str(entry.get("type")): dict(entry) for entry in existing or []
    }
    for update in updates:
        type_ = str(update["type"])
        previous = by_type.get(type_)
        if previous and previous.get("status") == update.get("status"):
            # Same status: keep the original transition time, refresh the detail.
            update = {**update, "lastTransitionTime": previous.get(
                "lastTransitionTime", update["lastTransitionTime"]
            )}
        by_type[type_] = update

    ordered = [by_type.pop(type_) for type_ in _CONDITION_ORDER if type_ in by_type]
    ordered.extend(by_type[type_] for type_ in sorted(by_type))
    return ordered


def ready(message: str, generation: int | None = None) -> dict[str, Any]:
    return condition(
        COND_READY, "True", "Reconciled", message, observed_generation=generation
    )


def not_ready(reason: str, message: str, generation: int | None = None) -> dict[str, Any]:
    return condition(COND_READY, "False", reason, message, observed_generation=generation)


def reachable(message: str, generation: int | None = None) -> dict[str, Any]:
    return condition(
        COND_REACHABLE, "True", "Connected", message, observed_generation=generation
    )


def unreachable(reason: str, message: str, generation: int | None = None) -> dict[str, Any]:
    return condition(
        COND_REACHABLE, "False", reason, message, observed_generation=generation
    )


def synced(message: str = "", generation: int | None = None) -> dict[str, Any]:
    return condition(
        COND_SYNCED, "True", "InSync", message, observed_generation=generation
    )


def not_synced(reason: str, message: str, generation: int | None = None) -> dict[str, Any]:
    return condition(COND_SYNCED, "False", reason, message, observed_generation=generation)


def drifted(
    message: str,
    generation: int | None = None,
    reason: str = "RetainedOrphans",
) -> dict[str, Any]:
    """Set when live state diverges from the spec in a way policy forbids fixing.

    A retained schema that has left the spec is the common case, hence the
    default reason: the operator will not drop it, so it says so rather than
    silently converging. Divergence the operator *cannot* fix rather than *will
    not* - an unownable schema, a parameter it may not set, an immutable field -
    passes its own reason, so the condition never misattributes the cause.
    """
    return condition(
        COND_DRIFTED, "True", reason, message, observed_generation=generation
    )


def no_drift(generation: int | None = None) -> dict[str, Any]:
    return condition(
        COND_DRIFTED, "False", "NoDrift", "live state matches spec",
        observed_generation=generation,
    )


def phase_from_conditions(conditions: list[dict[str, Any]]) -> str:
    """Collapse conditions into a single printer-column phase."""
    by_type = {str(entry.get("type")): entry for entry in conditions}
    ready_cond = by_type.get(COND_READY)
    if ready_cond is None:
        return "Pending"
    if ready_cond.get("status") == "True":
        drift = by_type.get(COND_DRIFTED)
        if drift is not None and drift.get("status") == "True":
            return "Drifted"
        return "Ready"
    if ready_cond.get("reason") in {"Paused"}:
        return "Paused"
    if by_type.get(COND_REACHABLE, {}).get("status") == "False":
        return "Unreachable"
    return "Failed"
