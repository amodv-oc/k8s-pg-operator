"""The PostgresReplication spec: defaults, validation, names, row filters, units."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from pg_operator.models import RowFilter, parse_replication_spec
from pg_operator.naming import InvalidIdentifier, replication_names
from pg_operator.postgres.filters import render_row_filter
from pg_operator.units import parse_duration, parse_quantity


def spec(**overrides: Any) -> dict[str, Any]:
    return {
        "source": {"dbRef": {"name": "orders"}},
        "target": {"dbRef": {"name": "orders-copy"}},
        "schemas": [{"name": "public"}],
        **overrides,
    }


# --- defaults and validation ------------------------------------------------


def test_defaults_follow_the_design() -> None:
    parsed = parse_replication_spec(spec())
    assert parsed.publish == ["insert", "update", "delete", "truncate"]
    assert parsed.copy_data is True
    assert parsed.enabled is True
    assert parsed.source.connection.ssl_mode == "require"
    assert parsed.source.connection.host is None
    assert parsed.source.credentials_secret_ref is None
    assert parsed.subscription.streaming is None
    assert parsed.alerting.max_lag_bytes == 1024**3
    assert parsed.alerting.max_retained_wal_bytes == 10 * 1024**3
    assert parsed.alerting.max_slot_inactive_seconds == 600
    assert parsed.publishes_keyed_changes is True


def test_something_must_be_selected() -> None:
    with pytest.raises(ValidationError, match="at least one entry"):
        parse_replication_spec(spec(schemas=[]))


def test_a_table_cannot_be_listed_and_excluded() -> None:
    with pytest.raises(ValidationError, match=r"public.sessions is both excluded"):
        parse_replication_spec(
            spec(
                schemas=[{"name": "public", "excludeTables": ["sessions"]}],
                tables=[{"schema": "public", "name": "sessions"}],
            )
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"schemas": [{"name": "public"}, {"name": "public"}]},
        {"tables": [{"schema": "a", "name": "t"}, {"schema": "a", "name": "t"}]},
        {"publish": ["insert", "insert"]},
        {"publish": []},
        {"schemas": [{"name": "Public"}]},
        {"tables": [{"schema": "a", "name": "t", "columns": ["id", "id"]}]},
    ],
)
def test_malformed_selections_are_rejected(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        parse_replication_spec(spec(**overrides))


def test_selected_schemas_cover_whole_and_partial_selections() -> None:
    parsed = parse_replication_spec(
        spec(schemas=[{"name": "audit"}], tables=[{"schema": "billing", "name": "invoices"}])
    )
    assert parsed.selected_schemas == {"audit", "billing"}


def test_insert_only_publication_needs_no_replica_identity() -> None:
    assert parse_replication_spec(spec(publish=["insert"])).publishes_keyed_changes is False


def test_verify_modes_need_a_ca_path_on_the_target_server() -> None:
    with pytest.raises(ValidationError, match="sslRootCertPath"):
        parse_replication_spec(spec(source={"dbRef": {"name": "orders"}, "connection": {"sslMode": "verify-full"}}))
    parse_replication_spec(
        spec(
            source={
                "dbRef": {"name": "orders"},
                "connection": {"sslMode": "verify-full", "sslRootCertPath": "/etc/ssl/rds.pem"},
            }
        )
    )


def test_unknown_fields_are_rejected_at_depth() -> None:
    with pytest.raises(ValidationError, match="where"):
        parse_replication_spec(spec(tables=[{"schema": "a", "name": "t", "where": "true"}]))


@pytest.mark.parametrize(
    ("alerting", "message"),
    [
        ({"maxLag": "0"}, "maxLag must be greater than zero"),
        ({"maxLag": "lots"}, "not a number"),
        ({"maxSlotInactive": "0s"}, "maxSlotInactive must be greater than zero"),
        ({"maxSlotInactive": "1d"}, "hours, minutes and/or seconds"),
    ],
)
def test_alerting_thresholds_are_validated(alerting: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        parse_replication_spec(spec(alerting=alerting))


# --- row filters --------------------------------------------------------------


def row_filter(**body: Any) -> RowFilter:
    return RowFilter.model_validate(body)


@pytest.mark.parametrize(
    ("predicate", "rendered"),
    [
        ({"column": "region", "op": "Eq", "value": "AU"}, "(\"region\" = 'AU')"),
        ({"column": "region", "op": "Ne", "value": "AU"}, "(\"region\" <> 'AU')"),
        ({"column": "total", "op": "Lt", "value": 10}, '("total" < 10)'),
        ({"column": "total", "op": "Le", "value": 10}, '("total" <= 10)'),
        ({"column": "total", "op": "Gt", "value": "10"}, "(\"total\" > '10')"),
        ({"column": "total", "op": "Ge", "value": 10}, '("total" >= 10)'),
        ({"column": "region", "op": "In", "values": ["AU", 3]}, "(\"region\" IN ('AU', 3))"),
        ({"column": "voided_at", "op": "IsNull"}, '("voided_at" IS NULL)'),
        ({"column": "voided_at", "op": "IsNotNull"}, '("voided_at" IS NOT NULL)'),
    ],
)
def test_each_operator_renders_as_sql(predicate: dict[str, Any], rendered: str) -> None:
    assert render_row_filter(row_filter(all=[predicate])).as_string(None) == f"({rendered})"


def test_all_joins_with_and_and_any_with_or() -> None:
    a = {"column": "a", "op": "IsNull"}
    b = {"column": "b", "op": "IsNull"}
    assert render_row_filter(row_filter(all=[a, b])).as_string(None) == '(("a" IS NULL) AND ("b" IS NULL))'
    assert render_row_filter(row_filter(any=[a, b])).as_string(None) == '(("a" IS NULL) OR ("b" IS NULL))'


@pytest.mark.parametrize(
    "hostile",
    [
        "AU') OR (true",
        "AU'); DROP TABLE orders; --",
        "x$$ OR true $$",
        "\\'; SELECT 1; --",
    ],
)
def test_an_operand_cannot_escape_its_literal(hostile: str) -> None:
    """Whatever an operand holds, it stays one literal denoting exactly itself."""
    rendered = render_row_filter(row_filter(all=[{"column": "region", "op": "Eq", "value": hostile}]))
    text = rendered.as_string(None)
    assert text.startswith('(("region" = ') and text.endswith("))")
    assert _decode_literal(text[len('(("region" = ') : -2]) == hostile


def _decode_literal(text: str) -> str:
    """Decode one SQL string literal, failing if anything follows its close."""
    text = text.strip()
    escaped = text.startswith("E'")
    body = text[2:] if escaped else text[1:]
    decoded: list[str] = []
    index = 0
    while index < len(body):
        char = body[index]
        if escaped and char == "\\":
            decoded.append(body[index + 1])
            index += 2
        elif char == "'" and body[index + 1 : index + 2] == "'":
            decoded.append("'")
            index += 2
        elif char == "'":
            assert index == len(body) - 1, f"SQL continues after the literal: {body[index + 1 :]!r}"
            return "".join(decoded)
        else:
            decoded.append(char)
            index += 1
    raise AssertionError(f"unterminated literal: {text!r}")


def test_column_names_must_be_plain_identifiers() -> None:
    """The model refuses a column that would need quoting, before SQL is built."""
    with pytest.raises(ValidationError):
        row_filter(all=[{"column": 'a" OR "b', "op": "IsNull"}])


@pytest.mark.parametrize(
    "predicate",
    [
        {"column": "a", "op": "In", "value": "x"},
        {"column": "a", "op": "In"},
        {"column": "a", "op": "Eq"},
        {"column": "a", "op": "Eq", "values": ["x"]},
        {"column": "a", "op": "IsNull", "value": "x"},
        {"column": "a", "op": "Eq", "value": True},
    ],
)
def test_operands_must_fit_the_operator(predicate: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        row_filter(all=[predicate])


def test_a_filter_sets_exactly_one_of_all_and_any() -> None:
    a = {"column": "a", "op": "IsNull"}
    with pytest.raises(ValidationError, match="exactly one of all or any"):
        row_filter(all=[a], any=[a])
    with pytest.raises(ValidationError, match="exactly one of all or any"):
        row_filter()


def test_filter_columns_are_what_the_replica_identity_must_cover() -> None:
    parsed = row_filter(any=[{"column": "region", "op": "Eq", "value": "AU"}, {"column": "id", "op": "IsNull"}])
    assert parsed.columns == {"region", "id"}


# --- names ----------------------------------------------------------------------


def test_names_are_deterministic_and_readable() -> None:
    names = replication_names("team-a", "orders-to-reporting")
    assert names == replication_names("team-a", "orders-to-reporting")
    assert names.publication.startswith("pgop_team_a_orders_to_reporting_")
    assert names.subscription == names.publication
    assert names.login.startswith("pgop_repl_team_a_orders_to_reporting_")
    assert names.secret == "pgop-repl.team-a.orders-to-reporting"


def test_hyphen_mapping_cannot_collide_two_replications() -> None:
    """team-a/x and team/a-x sanitise alike; the identity hash keeps them apart."""
    first = replication_names("team-a", "x")
    second = replication_names("team", "a-x")
    assert first.subscription != second.subscription
    assert first.login != second.login
    assert first.secret != second.secret


def test_the_role_prefix_separates_environments() -> None:
    plain = replication_names("team-a", "copy")
    staged = replication_names("team-a", "copy", "stg_")
    assert staged.subscription.startswith("pgop_stg_team_a_copy_")
    assert staged.login.startswith("stg_pgop_repl_team_a_copy_")
    assert staged.subscription != plain.subscription


def test_long_names_fit_and_stay_distinct() -> None:
    namespace = "n" * 63
    first = replication_names(namespace, "r" * 200 + "-one")
    second = replication_names(namespace, "r" * 200 + "-two")
    for names in (first, second):
        assert len(names.subscription.encode()) <= 63
        assert len(names.login.encode()) <= 63
        assert len(names.secret) <= 253
    assert first.subscription != second.subscription
    assert first.secret != second.secret


def test_names_stay_out_of_reserved_namespaces() -> None:
    """RDS refuses slots named rds*; PostgreSQL reserves pg_ for roles."""
    names = replication_names("rds-team", "copy")
    assert not names.subscription.startswith(("rds", "pg_"))
    with pytest.raises(InvalidIdentifier, match="pg_"):
        replication_names("team-a", "copy", "pg_")


# --- units ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1024, 1024),
        ("1024", 1024),
        ("1Ki", 1024),
        ("1.5Gi", 1536 * 1024**2),
        ("10Gi", 10 * 1024**3),
        ("2k", 2000),
        ("1G", 1000**3),
    ],
)
def test_quantities(value: int | str, expected: int) -> None:
    assert parse_quantity(value) == expected


@pytest.mark.parametrize("value", ["1GB", "-1", "1 Gi", "Gi", ""])
def test_malformed_quantities(value: str) -> None:
    with pytest.raises(ValueError, match="quantity"):
        parse_quantity(value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [("90s", 90), ("10m", 600), ("1h", 3600), ("1h30m", 5400), ("2h0m5s", 7205)],
)
def test_durations(value: str, expected: int) -> None:
    assert parse_duration(value) == expected


@pytest.mark.parametrize("value", ["", "10", "1d", "30m1h", "1.5h"])
def test_malformed_durations(value: str) -> None:
    with pytest.raises(ValueError, match="duration"):
        parse_duration(value)
