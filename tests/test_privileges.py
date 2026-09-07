"""Rendered SQL for the privilege model.

These assert the exact statements, which is the layer where a wrong keyword or a
missing role would be silently accepted by PostgreSQL and produce the wrong
access. Identifier quoting is checked here too, since it is the injection
boundary.
"""

from __future__ import annotations

import pytest

from pg_operator.postgres.privileges import (
    GrantOptions,
    GroupRoles,
    apply_default_privileges,
    apply_schema_privileges,
)

ROLES = GroupRoles(owner="app_owner", read_write="app_rw", read_only="app_ro")


class RecordingConnection:
    """Captures rendered SQL instead of executing it."""

    def __init__(self) -> None:
        self.statements: list[str] = []

    def cursor(self) -> RecordingConnection:
        return self

    async def __aenter__(self) -> RecordingConnection:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def execute(self, statement, params=None) -> None:
        del params
        self.statements.append(statement.as_string())


@pytest.fixture
def conn() -> RecordingConnection:
    return RecordingConnection()


async def test_owner_gets_all_on_the_schema(conn: RecordingConnection) -> None:
    await apply_schema_privileges(conn, "public", ROLES, GrantOptions())
    assert 'GRANT ALL ON SCHEMA "public" TO "app_owner"' in conn.statements


async def test_readers_get_usage_not_create(conn: RecordingConnection) -> None:
    await apply_schema_privileges(conn, "public", ROLES, GrantOptions())
    assert 'GRANT USAGE ON SCHEMA "public" TO "app_rw", "app_ro"' in conn.statements
    # Only the owner may create objects.
    assert not any(
        'CREATE ON SCHEMA "public" TO "app_rw"' in s for s in conn.statements
    )


async def test_read_write_privileges_are_exactly_the_dml_set(
    conn: RecordingConnection,
) -> None:
    await apply_schema_privileges(conn, "public", ROLES, GrantOptions())
    grant = next(
        s for s in conn.statements if 'ALL TABLES IN SCHEMA "public" TO "app_rw"' in s
    )
    assert grant == (
        'GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES '
        'ON ALL TABLES IN SCHEMA "public" TO "app_rw"'
    )


def _read_only_table_grant(statements: list[str]) -> str:
    return next(
        s for s in statements if 'ALL TABLES IN SCHEMA "public" TO "app_ro"' in s
    )


async def test_read_only_gets_select_only(conn: RecordingConnection) -> None:
    await apply_schema_privileges(conn, "public", ROLES, GrantOptions())
    assert (
        _read_only_table_grant(conn.statements)
        == 'GRANT SELECT ON ALL TABLES IN SCHEMA "public" TO "app_ro"'
    )


async def test_read_write_gets_sequence_usage(conn: RecordingConnection) -> None:
    """Needed to insert into a serial/identity column."""
    await apply_schema_privileges(conn, "public", ROLES, GrantOptions())
    grant = next(s for s in conn.statements if 'ALL SEQUENCES' in s and '"app_rw"' in s)
    assert grant == (
        'GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA "public" TO "app_rw"'
    )


async def test_read_only_cannot_advance_a_sequence(conn: RecordingConnection) -> None:
    await apply_schema_privileges(conn, "public", ROLES, GrantOptions())
    grant = next(s for s in conn.statements if 'ALL SEQUENCES' in s and '"app_ro"' in s)
    assert grant == 'GRANT SELECT ON ALL SEQUENCES IN SCHEMA "public" TO "app_ro"'


async def test_public_create_is_revoked_by_default(conn: RecordingConnection) -> None:
    await apply_schema_privileges(conn, "audit", ROLES, GrantOptions())
    assert 'REVOKE CREATE ON SCHEMA "audit" FROM PUBLIC' in conn.statements


async def test_public_revoke_can_be_disabled(conn: RecordingConnection) -> None:
    await apply_schema_privileges(
        conn, "audit", ROLES, GrantOptions(revoke_public_schema_create=False)
    )
    assert not any("FROM PUBLIC" in s for s in conn.statements)


async def test_public_schema_revoke_is_skipped_on_pg15_and_later(
    conn: RecordingConnection,
) -> None:
    """PostgreSQL 15 already denies PUBLIC CREATE on `public`."""
    await apply_schema_privileges(
        conn, "public", ROLES, GrantOptions(public_schema_locked_by_default=True)
    )
    assert not any("FROM PUBLIC" in s for s in conn.statements)


async def test_other_schemas_are_still_revoked_on_pg15(
    conn: RecordingConnection,
) -> None:
    """The PG15 change applies only to `public`, not to schemas we create."""
    await apply_schema_privileges(
        conn, "audit", ROLES, GrantOptions(public_schema_locked_by_default=True)
    )
    assert 'REVOKE CREATE ON SCHEMA "audit" FROM PUBLIC' in conn.statements


async def test_read_only_execute_can_be_withheld(conn: RecordingConnection) -> None:
    await apply_schema_privileges(
        conn, "public", ROLES, GrantOptions(grant_execute_to_read_only=False)
    )
    assert not any("ALL FUNCTIONS" in s and '"app_ro"' in s for s in conn.statements)
    # Read-write keeps EXECUTE either way.
    assert any("ALL FUNCTIONS" in s and '"app_rw"' in s for s in conn.statements)


# --- default privileges ----------------------------------------------------


async def test_default_privileges_are_keyed_on_the_owner_group(
    conn: RecordingConnection,
) -> None:
    """This is what makes tables created later visible without re-granting."""
    await apply_default_privileges(conn, "public", ROLES, GrantOptions())
    assert all(
        s.startswith('ALTER DEFAULT PRIVILEGES FOR ROLE "app_owner" IN SCHEMA "public"')
        for s in conn.statements
    )


async def test_default_privileges_cover_every_object_class(
    conn: RecordingConnection,
) -> None:
    await apply_default_privileges(conn, "public", ROLES, GrantOptions())
    for target in ("ON TABLES", "ON SEQUENCES", "ON FUNCTIONS", "ON TYPES"):
        assert any(target in s for s in conn.statements), target


async def test_default_privileges_apply_to_both_reader_roles(
    conn: RecordingConnection,
) -> None:
    await apply_default_privileges(conn, "public", ROLES, GrantOptions())
    assert any('TO "app_rw"' in s for s in conn.statements)
    assert any('TO "app_ro"' in s for s in conn.statements)


async def test_extra_creators_get_their_own_default_privileges(
    conn: RecordingConnection,
) -> None:
    """For when setRoleForOwners is off and login roles own what they create."""
    await apply_default_privileges(
        conn, "public", ROLES, GrantOptions(), creators=["app_owner", "migrator"]
    )
    assert any('FOR ROLE "migrator"' in s for s in conn.statements)
    assert any('FOR ROLE "app_owner"' in s for s in conn.statements)


# --- quoting ---------------------------------------------------------------


async def test_identifiers_are_quoted_not_interpolated(
    conn: RecordingConnection,
) -> None:
    """A schema name is never concatenated into SQL text."""
    hostile = GroupRoles(owner='a"b', read_write="rw", read_only="ro")
    await apply_schema_privileges(conn, 'x"; DROP TABLE y; --', hostile, GrantOptions())
    rendered = "\n".join(conn.statements)
    # The quote is doubled and the whole name stays inside one quoted identifier.
    assert '"x""; DROP TABLE y; --"' in rendered
    assert '"a""b"' in rendered
    # No statement boundary was introduced.
    assert "DROP TABLE y;" not in rendered.replace('"x""; DROP TABLE y; --"', "")


def test_group_roles_mapping_round_trip() -> None:
    mapping = {"owner": "o", "readWrite": "w", "readOnly": "r"}
    roles = GroupRoles.from_mapping(mapping)
    assert roles.as_mapping() == mapping
    assert roles.for_level("readWrite") == "w"
    assert roles.readers == ["w", "r"]
    assert roles.all == ["o", "w", "r"]
