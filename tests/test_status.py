"""Condition merging and the phase a resource reports."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from pg_operator import status as st


def test_conditions_render_in_a_stable_order() -> None:
    merged = st.merge_conditions(
        [], [st.no_drift(1), st.ready("ok", 1), st.synced("ok", 1), st.reachable("pg", 1)]
    )
    assert [entry["type"] for entry in merged] == [
        "Ready",
        "Reachable",
        "Synced",
        "Drifted",
    ]


def test_transition_time_is_preserved_while_status_is_unchanged() -> None:
    first = st.merge_conditions([], [st.ready("ok", 1)])
    original = first[0]["lastTransitionTime"]

    # Same status, new message: the transition time must not move.
    second = st.merge_conditions(first, [st.ready("still ok", 2)])
    assert second[0]["lastTransitionTime"] == original
    assert second[0]["message"] == "still ok"
    assert second[0]["observedGeneration"] == 2


def test_transition_time_moves_when_status_flips() -> None:
    ready = st.merge_conditions([], [st.ready("ok", 1)])
    ready[0]["lastTransitionTime"] = "2020-01-01T00:00:00Z"

    failed = st.merge_conditions(ready, [st.not_ready("ConnectionFailed", "down", 2)])
    assert failed[0]["lastTransitionTime"] != "2020-01-01T00:00:00Z"
    assert failed[0]["status"] == "False"


def test_unknown_condition_types_are_kept_after_the_known_ones() -> None:
    merged = st.merge_conditions(
        [{"type": "Custom", "status": "True", "lastTransitionTime": "x"}],
        [st.ready("ok", 1)],
    )
    assert [entry["type"] for entry in merged] == ["Ready", "Custom"]


def test_phase_ready() -> None:
    conditions = st.merge_conditions([], [st.ready("ok", 1), st.no_drift(1)])
    assert st.phase_from_conditions(conditions) == "Ready"


def test_phase_drifted_takes_precedence_over_ready() -> None:
    """A retained orphan is still a working resource, but must be visible."""
    conditions = st.merge_conditions(
        [], [st.ready("ok", 1), st.drifted("schema audit retained", 1)]
    )
    assert st.phase_from_conditions(conditions) == "Drifted"


def test_phase_unreachable_when_the_server_is_down() -> None:
    conditions = st.merge_conditions(
        [],
        [
            st.unreachable("ConnectionFailed", "timeout", 1),
            st.not_ready("ConnectionFailed", "timeout", 1),
        ],
    )
    assert st.phase_from_conditions(conditions) == "Unreachable"


def test_phase_failed_for_a_non_connectivity_failure() -> None:
    conditions = st.merge_conditions(
        [], [st.reachable("pg 16", 1), st.not_ready("ConfigurationError", "bad spec", 1)]
    )
    assert st.phase_from_conditions(conditions) == "Failed"


def test_phase_paused() -> None:
    conditions = [st.condition("Ready", "False", "Paused", "suspended")]
    assert st.phase_from_conditions(conditions) == "Paused"


def test_phase_pending_without_a_ready_condition() -> None:
    assert st.phase_from_conditions([]) == "Pending"


def test_message_is_truncated_rather_than_rejected() -> None:
    entry = st.not_ready("Boom", "x" * 40_000, 1)
    assert len(entry["message"]) == 32_000


def test_timestamp_is_rfc3339_utc() -> None:
    value = st.now()
    assert value.endswith("Z")
    assert len(value) == 20


# ---------------------------------------------------------------------------
# database divergence reporting
# ---------------------------------------------------------------------------


def _db_status(**result_kwargs: object) -> dict[str, Any]:
    """Build a PostgresDB status from a synthetic reconcile result."""
    from pg_operator.postgres.privileges import GroupRoles
    from pg_operator.reconcile.database import DatabaseResult, build_status

    result = DatabaseResult(
        database="orders",
        roles=GroupRoles(owner="orders_owner", read_write="orders_rw", read_only="orders_ro"),
        **result_kwargs,  # type: ignore[arg-type]
    )
    instance = SimpleNamespace(
        name="pg16",
        display="pg16:5432",
        server=SimpleNamespace(version="16.15"),
    )
    return build_status(result, instance, None, 1)  # type: ignore[arg-type]


def _by_type(status: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {c["type"]: c for c in status["conditions"]}


def test_no_divergence_reports_in_sync() -> None:
    conditions = _by_type(_db_status())
    assert conditions["Drifted"]["status"] == "False"
    assert conditions["Synced"]["status"] == "True"


def test_retained_orphans_drift_but_do_not_clear_synced() -> None:
    """An orphan the policy forbids dropping is drift, not an unsatisfiable spec."""
    status = _db_status(orphaned_schemas=["legacy"], retention="RETAIN")
    conditions = _by_type(status)
    assert conditions["Drifted"]["reason"] == "RetainedOrphans"
    assert "nothing was dropped" in conditions["Drifted"]["message"]
    assert conditions["Synced"]["status"] == "True"
    assert status["phase"] == "Drifted"


def test_denied_parameter_names_itself_in_both_conditions() -> None:
    status = _db_status(denied_parameters=["parameter log_min_duration_statement ..."])
    conditions = _by_type(status)
    assert conditions["Drifted"]["reason"] == "ParameterDenied"
    assert conditions["Synced"]["reason"] == "ParameterDenied"
    # The pass still completed, so the resource is usable.
    assert conditions["Ready"]["status"] == "True"
    assert status["deniedParameters"] == ["parameter log_min_duration_statement ..."]


def test_unowned_schema_names_itself() -> None:
    conditions = _by_type(_db_status(unowned_schemas=["public"]))
    assert conditions["Drifted"]["reason"] == "SchemaNotOwned"
    assert conditions["Synced"]["reason"] == "SchemaNotOwned"


def test_several_causes_collapse_to_multiple_issues() -> None:
    """Each cause must survive into the message, not be hidden by the first."""
    conditions = _by_type(
        _db_status(unowned_schemas=["public"], denied_parameters=["parameter x denied"])
    )
    assert conditions["Drifted"]["reason"] == "MultipleIssues"
    assert conditions["Synced"]["reason"] == "MultipleIssues"
    assert "public" in conditions["Synced"]["message"]
    assert "parameter x denied" in conditions["Synced"]["message"]


# ---------------------------------------------------------------------------
# parameter application degrades rather than aborting
# ---------------------------------------------------------------------------


async def test_denied_parameter_does_not_stop_the_others() -> None:
    """One unsettable parameter must not cost the rest of the pass.

    The refusal is a permanent fact about the server's grant state, so retrying
    the whole database - schemas, extensions and grants included - would never
    make progress.
    """
    import pg_operator.postgres.database as pgdb
    from pg_operator.errors import InsufficientPrivilege

    attempted: list[str] = []

    async def fake_execute(conn: object, statement: object, *a: object, **kw: object) -> None:
        rendered = str(statement)
        attempted.append(rendered)
        if "log_min_duration_statement" in rendered:
            raise InsufficientPrivilege("permission denied to set parameter")

    original = pgdb.execute
    pgdb.execute = fake_execute  # type: ignore[assignment]
    try:
        outcome = await pgdb.apply_database_parameters(
            None,  # type: ignore[arg-type]
            "orders",
            {"log_min_duration_statement": "500", "statement_timeout": "30000"},
            {"work_mem": "64MB"},
        )
    finally:
        pgdb.execute = original  # type: ignore[assignment]

    assert outcome.changes == ["set statement_timeout=30000", "reset work_mem"]
    assert len(outcome.denied) == 1
    assert "log_min_duration_statement" in outcome.denied[0]
    # Every statement was still attempted; the refusal did not short-circuit.
    assert len(attempted) == 3
