"""The subscription's connection string - what the target server is told about the source - and its slots' names."""

from __future__ import annotations

import re

from psycopg.conninfo import conninfo_to_dict

from pg_operator.postgres.subscription import build_conninfo, conninfo_fingerprint, sync_slot_pattern


def conninfo(**overrides: str) -> str:
    settings = {
        "host": "prod.example.internal",
        "port": 5432,
        "database": "orders",
        "user": "pgop_repl_team_a_x",
        "password": "s3cret with spaces and 'quotes'",
        "ssl_mode": "require",
        "ssl_root_cert": None,
    }
    settings.update(overrides)
    return build_conninfo(**settings)  # type: ignore[arg-type]


def test_the_password_is_only_ever_proved_never_sent() -> None:
    """source.connection.host is free-form, and may not be the source at all.

    libpq sends a cleartext password to any server that asks for one, and
    sslmode=require checks no certificate - so without this a replication
    pointed at the wrong host hands that host the password.
    """
    parsed = conninfo_to_dict(conninfo())
    assert parsed["require_auth"] == "scram-sha-256"
    assert parsed["password"] == "s3cret with spaces and 'quotes'"
    assert parsed["connect_timeout"] == "10"


def test_the_fingerprint_follows_the_connection_and_hides_the_password() -> None:
    first, second = conninfo(), conninfo(password="another")
    assert conninfo_fingerprint(first) == conninfo_fingerprint(conninfo())
    assert conninfo_fingerprint(first) != conninfo_fingerprint(second)
    assert "s3cret" not in conninfo_fingerprint(first)


def test_table_sync_slots_are_matched_by_oid_and_by_the_subscriber() -> None:
    """pg_<subscription oid>_sync_<relid>_<system identifier>, oid and identifier both the target's.

    Another target subscribing to the same source can hold the same oid; its
    slots differ only in the last part.
    """
    ours = re.compile(sync_slot_pattern(16400, "7709052924873089057"))
    assert ours.match("pg_16400_sync_16384_7709052924873089057")
    for other in (
        "pg_16400_sync_16384_7709052924873089058",  # another target, the same oid
        "pg_16400_sync_16384_17709052924873089057",
        "pg_164001_sync_16384_7709052924873089057",
        "pg_1640_sync_16384_7709052924873089057",
        "pg_16400_sync_16384_7709052924873089057_x",
        "pg_16400_sync__7709052924873089057",
    ):
        assert not ours.match(other), other
    # Without the identifier, every subscriber's slots of that oid.
    anyone = re.compile(sync_slot_pattern(16400, None))
    assert anyone.match("pg_16400_sync_16384_7709052924873089058")
    assert not anyone.match("pg_164001_sync_16384_7709052924873089058")
