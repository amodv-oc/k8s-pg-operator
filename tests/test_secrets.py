"""Password generation and the shape of a generated credentials Secret."""

from __future__ import annotations

import re
from urllib.parse import unquote, urlsplit

import pytest

from pg_operator.constants import LABEL_MANAGED_BY, LABEL_MANAGED_BY_VALUE
from pg_operator.k8s.secrets import connection_keys, generate_password, managed_labels


def test_generated_password_has_the_requested_length() -> None:
    assert len(generate_password(32)) == 32
    assert len(generate_password(64)) == 64


def test_short_passwords_are_refused() -> None:
    with pytest.raises(ValueError, match="at least 16"):
        generate_password(8)


def test_password_alphabet_is_connection_string_safe() -> None:
    """No character that would need escaping in a libpq or JDBC URL."""
    for _ in range(50):
        assert re.fullmatch(r"[A-Za-z0-9\-_.~]+", generate_password(32))


def test_passwords_are_not_repeated() -> None:
    assert len({generate_password(24) for _ in range(100)}) == 100


def test_connection_keys_cover_the_documented_set() -> None:
    data = connection_keys(
        username="svc_api",
        password="s3cret",
        host="db.example.com",
        port=5432,
        database="orders",
        ssl_mode="require",
        include_uri=True,
    )
    assert set(data) == {
        "username",
        "password",
        "host",
        "port",
        "database",
        "sslmode",
        "uri",
        "jdbcUri",
    }
    assert data["port"] == "5432"


def test_uri_can_be_omitted() -> None:
    data = connection_keys(
        username="u",
        password="p",
        host="h",
        port=5432,
        database="d",
        ssl_mode="require",
        include_uri=False,
    )
    assert "uri" not in data and "jdbcUri" not in data


def test_uri_percent_encodes_special_characters() -> None:
    """An unencoded password would silently produce an unparseable URI."""
    data = connection_keys(
        username="user@corp",
        password="p@ss/word:1?2#3",
        host="db.example.com",
        port=5432,
        database="orders",
        ssl_mode="require",
        include_uri=True,
    )
    parsed = urlsplit(data["uri"])
    assert parsed.scheme == "postgresql"
    assert parsed.hostname == "db.example.com"
    assert parsed.port == 5432
    assert parsed.path == "/orders"
    # urlsplit returns the raw components, so decode to prove the credential
    # round-trips instead of corrupting the URI structure. A raw "/" or "?" in
    # the password would otherwise be read as the path or query.
    assert unquote(parsed.username or "") == "user@corp"
    assert unquote(parsed.password or "") == "p@ss/word:1?2#3"


def test_jdbc_uri_carries_credentials_as_parameters() -> None:
    data = connection_keys(
        username="svc_api",
        password="a b",
        host="h",
        port=5433,
        database="d",
        ssl_mode="verify-full",
        include_uri=True,
    )
    assert data["jdbcUri"].startswith("jdbc:postgresql://h:5433/d?")
    assert "password=a%20b" in data["jdbcUri"]
    assert "sslmode=verify-full" in data["jdbcUri"]


def test_managed_labels_identify_the_owner_across_namespaces() -> None:
    labels = managed_labels(
        owner_kind="PostgresUser",
        owner_name="orders-api",
        owner_namespace="team-a",
        instance="prod-rds",
        extra={"team": "payments"},
    )
    assert labels[LABEL_MANAGED_BY] == LABEL_MANAGED_BY_VALUE
    assert labels["postgres.opsplatform.io/owner-kind"] == "PostgresUser"
    assert labels["postgres.opsplatform.io/owner-name"] == "orders-api"
    assert labels["postgres.opsplatform.io/owner-namespace"] == "team-a"
    assert labels["postgres.opsplatform.io/instance"] == "prod-rds"
    assert labels["team"] == "payments"
