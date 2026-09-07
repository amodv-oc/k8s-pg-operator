"""Identifier derivation: the boundary between Kubernetes names and SQL."""

from __future__ import annotations

import pytest

from pg_operator.naming import (
    MAX_IDENTIFIER_LENGTH,
    InvalidIdentifier,
    group_role_names,
    sanitize_identifier,
    secret_name,
    validate_identifier,
    validate_role_name,
)


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("orders", "orders"),
        ("my-app", "my_app"),
        ("My-App.DB", "my_app_db"),
        ("team-a--orders", "team_a_orders"),
        ("1st-service", "st_service"),  # leading digits are not legal in an identifier
        ("--orders--", "orders"),
    ],
)
def test_sanitize_maps_k8s_names_to_identifiers(given: str, expected: str) -> None:
    assert sanitize_identifier(given) == expected


def test_sanitize_rejects_values_with_nothing_usable() -> None:
    for value in ("", "   ", "---", "123"):
        with pytest.raises(InvalidIdentifier):
            sanitize_identifier(value)


def test_long_names_truncate_and_stay_distinct() -> None:
    a = sanitize_identifier("x" * 70 + "-alpha")
    b = sanitize_identifier("x" * 70 + "-beta")
    assert len(a) <= MAX_IDENTIFIER_LENGTH
    assert len(b) <= MAX_IDENTIFIER_LENGTH
    # A shared 70-character prefix must not collapse into the same identifier.
    assert a != b


def test_sanitize_is_deterministic() -> None:
    assert sanitize_identifier("Some-Long-Name") == sanitize_identifier("Some-Long-Name")


@pytest.mark.parametrize("value", ["orders", "orders_2", "_private", "a$b"])
def test_validate_accepts_legal_identifiers(value: str) -> None:
    assert validate_identifier(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "Orders",          # uppercase would need quoting to round-trip
        "my-app",          # hyphen is not legal unquoted
        "2fast",           # cannot start with a digit
        'ev"il',           # quote
        "drop table x",    # whitespace
        "x" * 64,          # over NAMEDATALEN-1
        "",
    ],
)
def test_validate_rejects_anything_needing_quoting(value: str) -> None:
    with pytest.raises(InvalidIdentifier):
        validate_identifier(value)


def test_role_names_reject_the_reserved_prefix() -> None:
    with pytest.raises(InvalidIdentifier):
        validate_role_name("pg_secret")
    assert validate_role_name("pgx_ok") == "pgx_ok"


def test_group_role_names_follow_the_documented_pattern() -> None:
    assert group_role_names("orders") == {
        "owner": "orders_owner",
        "readWrite": "orders_rw",
        "readOnly": "orders_ro",
    }


def test_group_role_names_apply_a_prefix() -> None:
    assert group_role_names("orders", "staging_")["owner"] == "staging_orders_owner"


def test_group_role_names_stay_within_the_length_limit() -> None:
    names = group_role_names("d" * 70)
    assert all(len(name) <= MAX_IDENTIFIER_LENGTH for name in names.values())
    # All three must remain distinct after truncation, or grants would collide.
    assert len(set(names.values())) == 3


def test_secret_name_default_and_truncation() -> None:
    assert secret_name("orders-api") == "orders-api-pg-credentials"
    assert len(secret_name("n" * 300)) <= 253
