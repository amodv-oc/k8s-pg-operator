"""Which tables a publication may carry - every reason, without a server."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from pg_operator.models import RowFilter, parse_replication_spec
from pg_operator.postgres.publication import SourceTable, TargetTable
from pg_operator.reconcile import eligibility as el


def source(
    name: str = "orders",
    schema: str = "public",
    *,
    columns: dict[str, str] | None = None,
    identity: frozenset[str] | None = frozenset({"id"}),
    **overrides: Any,
) -> SourceTable:
    cols = columns if columns is not None else {"id": "integer", "region": "text", "total": "numeric"}
    fields: dict[str, Any] = {
        "oid": 1,
        "schema": schema,
        "name": name,
        "kind": "r",
        "persistent": True,
        "is_partition": False,
        "owner": "orders_owner",
        "owner_usable": True,
        "row_security": False,
        "replica_identity": "d",
        "identity_columns": identity,
        "columns": cols,
        "generated": frozenset(),
        "readable_by_login": True,
        "leaves": (),
    }
    fields.update(overrides)
    return SourceTable(**fields)


def target(
    name: str = "orders",
    schema: str = "public",
    *,
    identity: frozenset[str] | None = frozenset({"id"}),
    **overrides: Any,
) -> TargetTable:
    fields: dict[str, Any] = {
        "schema": schema,
        "name": name,
        "kind": "r",
        "owner": "orders_replica_owner",
        "owner_settable": True,
        "columns": {"id": "integer", "region": "text", "total": "numeric"},
        "generated": frozenset(),
        "identity_columns": identity,
        "required_columns": frozenset(),
    }
    fields.update(overrides)
    return TargetTable(**fields)


def candidate(name: str = "orders", **fields: Any) -> el.Candidate:
    return el.Candidate("public", name, **fields)


def judge(
    candidate_: el.Candidate,
    source_: SourceTable | None,
    targets: list[TargetTable] | None = None,
    **options: Any,
) -> el.Verdict:
    settings: dict[str, Any] = {
        "keyed": True,
        "via_root": False,
        "owner_group": "orders_owner",
        "read_group": "orders_ro",
        "login": "pgop_repl_x",
    }
    settings.update(options)
    found = {t.qualified: t for t in (targets if targets is not None else [target()])}
    return el.judge(candidate_, source_, found, **settings)


def reasons(verdict: el.Verdict) -> list[str]:
    return [issue.reason for issue in verdict.issues]


def filter_on(*columns: str) -> RowFilter:
    return RowFilter.model_validate({"all": [{"column": c, "op": "IsNotNull"} for c in columns]})


# --- the happy path, and what never qualifies --------------------------------


def test_a_keyed_table_both_sides_can_take_is_publishable() -> None:
    verdict = judge(candidate(), source())
    assert verdict.publishable and verdict.issues == () and verdict.warnings == ()


def test_a_missing_source_table_is_named() -> None:
    assert reasons(judge(candidate(explicit=True), None)) == [el.SOURCE_TABLE_MISSING]


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"kind": "v"}, "a view"),
        ({"kind": "m"}, "a materialised view"),
        ({"kind": "f"}, "a foreign table"),
        ({"persistent": False}, "unlogged or temporary"),
        ({"is_partition": True}, "is a partition"),
    ],
)
def test_only_permanent_tables_are_publishable(overrides: dict[str, Any], fragment: str) -> None:
    verdict = judge(candidate(explicit=True), source(**overrides))
    assert reasons(verdict) == [el.NOT_PUBLISHABLE]
    assert fragment in verdict.issues[0].message


# --- replica identity: what keeps the source's own writes working ------------


def test_no_replica_identity_would_break_source_writes() -> None:
    verdict = judge(candidate(), source(identity=None))
    assert reasons(verdict) == [el.NO_REPLICA_IDENTITY]
    assert "UPDATE and DELETE on the source fail" in verdict.issues[0].message


def test_an_insert_only_publication_needs_no_replica_identity() -> None:
    assert judge(candidate(), source(identity=None), keyed=False).publishable


def test_a_filter_may_read_only_identity_columns() -> None:
    assert judge(candidate(row_filter=filter_on("id")), source()).publishable
    verdict = judge(candidate(row_filter=filter_on("region")), source())
    assert reasons(verdict) == [el.ROW_FILTER_NOT_COVERED]


def test_replica_identity_full_covers_every_column() -> None:
    full = source(identity=frozenset({"id", "region", "total"}), replica_identity="f")
    assert judge(candidate(row_filter=filter_on("region")), full).publishable


def test_a_column_list_must_include_the_identity() -> None:
    assert judge(candidate(columns=("id", "region")), source()).publishable
    verdict = judge(candidate(columns=("region",)), source())
    assert reasons(verdict) == [el.COLUMN_LIST_MISSES_IDENTITY]


def test_identity_is_irrelevant_to_filters_without_keyed_changes() -> None:
    assert judge(candidate(row_filter=filter_on("region")), source(), keyed=False).publishable


# --- columns ---------------------------------------------------------------------


def test_unknown_and_generated_columns_are_named() -> None:
    table = source(generated=frozenset({"slug"}))
    verdict = judge(candidate(columns=("id", "slug", "nope")), table)
    assert reasons(verdict) == [el.UNKNOWN_COLUMN]
    assert "nope, slug" in verdict.issues[0].message
    assert "slug generated" in verdict.issues[0].message


# --- source privileges ---------------------------------------------------------


def test_an_unusable_owner_names_the_fix() -> None:
    verdict = judge(candidate(), source(owner="dba", owner_usable=False))
    assert reasons(verdict) == [el.SOURCE_TABLE_NOT_OWNED]
    assert "OWNER TO orders_owner" in verdict.issues[0].message


def test_an_unreadable_table_names_the_group_to_grant() -> None:
    verdict = judge(candidate(), source(readable_by_login=False))
    assert reasons(verdict) == [el.SOURCE_TABLE_NOT_READABLE]
    assert "orders_ro" in verdict.issues[0].message


def test_row_level_security_would_leave_a_partial_replica() -> None:
    assert reasons(judge(candidate(), source(row_security=True))) == [el.ROW_SECURITY_ENABLED]


# --- the target ------------------------------------------------------------------


def test_a_table_missing_on_the_target_is_held_back() -> None:
    verdict = judge(candidate(), source(), targets=[])
    assert reasons(verdict) == [el.TARGET_TABLE_MISSING]
    assert verdict.issues[0].side == "target"


def test_a_view_on_the_target_is_not_a_table() -> None:
    assert reasons(judge(candidate(), source(), targets=[target(kind="v")])) == [el.TARGET_TABLE_MISSING]


def test_published_columns_must_exist_on_the_target_and_not_be_generated() -> None:
    lacking = target(columns={"id": "integer", "region": "text"})
    assert reasons(judge(candidate(), source(), targets=[lacking])) == [el.TARGET_COLUMNS_MISSING]
    generated = target(columns={"id": "integer", "region": "text"}, generated=frozenset({"total"}))
    assert reasons(judge(candidate(), source(), targets=[generated])) == [el.TARGET_COLUMNS_MISSING]
    # A column list narrows what the target needs.
    assert judge(candidate(columns=("id", "region")), source(), targets=[lacking]).publishable


def test_extra_target_columns_are_fine() -> None:
    wider = target(columns={"id": "integer", "region": "text", "total": "numeric", "note": "text"})
    assert judge(candidate(), source(), targets=[wider]).publishable


def test_a_target_that_cannot_identify_a_row_stops_the_whole_subscription() -> None:
    """PostgreSQL refuses the apply, and apply is sequential: one table stops all of them."""
    verdict = judge(candidate(), source(), targets=[target(identity=None)])
    assert reasons(verdict) == [el.TARGET_NO_REPLICA_IDENTITY]
    # Target-sided, so a table already published stays published (design D6).
    assert verdict.issues[0].side == "target" and verdict.keeps_membership
    assert "primary key" in verdict.issues[0].message


def test_a_publisher_sending_full_rows_asks_nothing_of_the_target() -> None:
    whole_row = source(replica_identity="f", identity=frozenset({"id", "region", "total"}))
    assert judge(candidate(), whole_row, targets=[target(identity=None)]).publishable


def test_a_source_with_no_identity_is_not_reported_against_the_target_as_well() -> None:
    """One typo, one cause: NoReplicaIdentity already names the end that must change."""
    verdict = judge(candidate(), source(identity=None), targets=[target(identity=None)])
    assert reasons(verdict) == [el.NO_REPLICA_IDENTITY]


def test_a_target_identity_wider_than_the_publishers_is_held_back() -> None:
    """The publisher sends its own identity as the key, whatever else it publishes."""
    verdict = judge(candidate(), source(), targets=[target(identity=frozenset({"id", "region"}))])
    assert reasons(verdict) == [el.TARGET_IDENTITY_NOT_SENT]
    assert "region" in verdict.issues[0].message


def test_a_target_identity_of_fewer_columns_is_fine() -> None:
    wider_source = source(identity=frozenset({"id", "region"}))
    assert judge(candidate(), wider_source, targets=[target(identity=frozenset({"id"}))]).publishable


def test_the_target_needs_no_identity_for_an_insert_only_publication() -> None:
    verdict = judge(candidate(), source(), targets=[target(identity=None)], keyed=False)
    assert verdict.publishable


def test_a_required_target_column_the_publication_does_not_carry_is_held_back() -> None:
    """Every INSERT the subscription applies would fail the NOT NULL."""
    needs_tenant = target(
        columns={"id": "integer", "region": "text", "total": "numeric", "tenant": "text"},
        required_columns=frozenset({"tenant"}),
    )
    verdict = judge(candidate(), source(), targets=[needs_tenant])
    assert reasons(verdict) == [el.TARGET_COLUMN_REQUIRED]
    assert verdict.issues[0].side == "target" and "tenant" in verdict.issues[0].message


def test_a_required_target_column_is_fine_once_the_publication_carries_it() -> None:
    needs_region = target(required_columns=frozenset({"region"}))
    assert judge(candidate(), source(), targets=[needs_region]).publishable
    # ...but not if a column list leaves it out.
    narrowed = judge(candidate(columns=("id", "total")), source(), targets=[needs_region])
    assert reasons(narrowed) == [el.TARGET_COLUMN_REQUIRED]


def test_an_owner_the_managing_role_cannot_become_is_held_back() -> None:
    verdict = judge(candidate(), source(), targets=[target(owner_settable=False)])
    assert reasons(verdict) == [el.TARGET_OWNER_NOT_SETTABLE]


def test_differing_column_types_only_warn() -> None:
    widened = target(columns={"id": "bigint", "region": "text", "total": "numeric"})
    verdict = judge(candidate(), source(), targets=[widened])
    assert verdict.publishable
    assert [w.reason for w in verdict.warnings] == [el.COLUMN_TYPE_MISMATCH]
    assert "id integer -> bigint" in verdict.warnings[0].message


def test_a_table_claimed_by_an_older_replication_is_refused() -> None:
    verdict = judge(candidate(), source(), claimed_by="team-a/older")
    assert reasons(verdict) == [el.TABLE_TARGETED_TWICE]
    assert "team-a/older" in verdict.issues[0].message


# --- what happens to a table that is already published (design D6) -------------


def test_target_side_problems_keep_a_published_table_published() -> None:
    verdict = judge(candidate(), source(), targets=[])
    assert not verdict.publishable and verdict.keeps_membership


def test_source_side_problems_take_a_published_table_out() -> None:
    verdict = judge(candidate(), source(identity=None), targets=[])
    assert not verdict.keeps_membership


def test_a_claimed_table_is_taken_out_even_though_the_claim_is_target_side() -> None:
    assert not judge(candidate(), source(), claimed_by="team-a/older").keeps_membership


# --- partitioned tables ----------------------------------------------------------


def partitioned(*leaves: SourceTable) -> SourceTable:
    return source("events", kind="p", identity=None, leaves=leaves)


def leaf(name: str, **overrides: Any) -> SourceTable:
    return source(name, is_partition=True, **overrides)


def test_without_publish_via_root_the_target_needs_every_leaf() -> None:
    root = partitioned(leaf("events_2025"), leaf("events_2026"))
    present = [target("events"), target("events_2025")]
    verdict = judge(candidate("events"), root, targets=present)
    assert reasons(verdict) == [el.TARGET_TABLE_MISSING]
    assert "public.events_2026" in verdict.issues[0].message


def test_with_publish_via_root_the_target_needs_only_the_root() -> None:
    root = partitioned(leaf("events_2025"))
    assert judge(candidate("events"), root, targets=[target("events")], via_root=True).publishable


def test_each_leaf_needs_a_replica_identity() -> None:
    root = partitioned(leaf("events_2025"), leaf("events_2026", identity=None))
    verdict = judge(
        candidate("events"),
        root,
        targets=[target("events_2025"), target("events_2026")],
    )
    assert reasons(verdict) == [el.NO_REPLICA_IDENTITY]
    assert "events_2026" in verdict.issues[0].message


def test_a_filter_on_a_partitioned_table_needs_publish_via_root() -> None:
    """Without it each leaf is published unfiltered: every row would flow, silently."""
    root = partitioned(leaf("events_2025"))
    verdict = judge(
        candidate("events", row_filter=filter_on("id")), root, targets=[target("events_2025")]
    )
    assert el.FILTER_IGNORED_WITHOUT_ROOT in reasons(verdict)
    assert judge(
        candidate("events", row_filter=filter_on("id")), root, targets=[target("events")], via_root=True
    ).publishable


# --- candidates --------------------------------------------------------------------


def test_schemas_expand_to_their_permanent_tables_minus_exclusions() -> None:
    spec = parse_replication_spec(
        {
            "source": {"dbRef": {"name": "a"}},
            "target": {"dbRef": {"name": "b"}},
            "schemas": [{"name": "public", "excludeTables": ["sessions"]}],
            "tables": [{"schema": "public", "name": "orders", "columns": ["id"]}],
        }
    )
    catalog = {
        t.qualified: t
        for t in (
            source("orders"),
            source("sessions"),
            source("events", kind="p"),
            source("events_2026", is_partition=True),
            source("summary", kind="m"),
            source("scratch", persistent=False),
            source("elsewhere", schema="audit"),
        )
    }
    found = el.candidates(spec, catalog)
    assert [c.qualified for c in found] == ["public.events", "public.orders"]
    explicit = next(c for c in found if c.name == "orders")
    assert explicit.explicit and explicit.columns == ("id",)


def test_an_explicit_entry_is_judged_even_when_unpublishable() -> None:
    spec = parse_replication_spec(
        {
            "source": {"dbRef": {"name": "a"}},
            "target": {"dbRef": {"name": "b"}},
            "tables": [{"schema": "public", "name": "summary"}],
        }
    )
    found = el.candidates(spec, {"public.summary": source("summary", kind="m")})
    assert [c.qualified for c in found] == ["public.summary"]


def test_target_relations_follow_publish_via_root() -> None:
    root = partitioned(leaf("events_2025"))
    assert [t.name for t in el.target_relations(root, via_root=False)] == ["events_2025"]
    assert [t.name for t in el.target_relations(root, via_root=True)] == ["events"]
    plain = source()
    assert el.target_relations(plain, via_root=False) == (plain,)
    assert replace(plain, kind="p", leaves=()).kind == "p"


# --- the initial copy ----------------------------------------------------------


def test_a_new_table_with_rows_on_the_target_waits() -> None:
    verdict = judge(candidate(), source(), copy_data=True, target_nonempty=frozenset({"public.orders"}))
    assert reasons(verdict) == [el.TARGET_TABLE_NOT_EMPTY]
    assert verdict.keeps_membership


def test_rows_on_the_target_are_fine_once_subscribed_or_without_a_copy() -> None:
    nonempty = frozenset({"public.orders"})
    subscribed = frozenset({"public.orders"})
    assert judge(candidate(), source(), copy_data=True, target_nonempty=nonempty, subscribed=subscribed).publishable
    assert judge(candidate(), source(), copy_data=False, target_nonempty=nonempty).publishable
