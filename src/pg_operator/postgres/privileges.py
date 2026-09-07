"""The OWNER / RW / RO privilege model.

Each PostgresDB is backed by three NOLOGIN group roles. Login roles never
receive object privileges directly — they are granted membership of a group, so
a privilege fix applies to every member at once and no per-user re-granting is
needed when a schema is added.

    <db>_owner   full DDL on the database and every managed schema
    <db>_rw      USAGE on schemas; SELECT/INSERT/UPDATE/DELETE on tables;
                 USAGE/SELECT on sequences; EXECUTE on routines
    <db>_ro      USAGE on schemas; SELECT on tables and sequences

Grants are applied twice for a reason:

* ``GRANT ... ON ALL TABLES IN SCHEMA`` covers objects that exist *now*.
* ``ALTER DEFAULT PRIVILEGES FOR ROLE <db>_owner`` covers objects created
  *later*. Default privileges are keyed on the creating role, which is why
  members of the owner group get ``role`` set per-database (see
  ``roles.set_default_role_in_database``) — otherwise a migration run by a human
  login role creates tables the RW role cannot see.

Both passes are idempotent, so a periodic reconcile is a no-op in steady state.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from psycopg import AsyncConnection, sql

from .sql import execute, fetch_all, ident, idents

log = logging.getLogger(__name__)

#: Table privileges by access level.
TABLE_PRIVILEGES: dict[str, tuple[str, ...]] = {
    "owner": ("ALL",),
    "readWrite": ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES"),
    "readOnly": ("SELECT",),
}

SEQUENCE_PRIVILEGES: dict[str, tuple[str, ...]] = {
    "owner": ("ALL",),
    "readWrite": ("USAGE", "SELECT", "UPDATE"),
    "readOnly": ("SELECT",),
}

ROUTINE_PRIVILEGES: dict[str, tuple[str, ...]] = {
    "owner": ("ALL",),
    "readWrite": ("EXECUTE",),
    "readOnly": ("EXECUTE",),
}

TYPE_PRIVILEGES: dict[str, tuple[str, ...]] = {
    "owner": ("ALL",),
    "readWrite": ("USAGE",),
    "readOnly": ("USAGE",),
}


@dataclass(frozen=True, slots=True)
class GroupRoles:
    """The three group roles backing one database."""

    owner: str
    read_write: str
    read_only: str

    @classmethod
    def from_mapping(cls, mapping: dict[str, str]) -> GroupRoles:
        return cls(
            owner=mapping["owner"],
            read_write=mapping["readWrite"],
            read_only=mapping["readOnly"],
        )

    def as_mapping(self) -> dict[str, str]:
        return {
            "owner": self.owner,
            "readWrite": self.read_write,
            "readOnly": self.read_only,
        }

    def for_level(self, level_key: str) -> str:
        return self.as_mapping()[level_key]

    @property
    def all(self) -> list[str]:
        return [self.owner, self.read_write, self.read_only]

    @property
    def readers(self) -> list[str]:
        """Roles that read but do not own — the ones needing explicit grants."""
        return [self.read_write, self.read_only]


@dataclass(frozen=True, slots=True)
class GrantOptions:
    """Knobs from PostgresDB.spec that change how grants are issued."""

    revoke_public_schema_create: bool = True
    grant_execute_to_read_only: bool = True
    #: PostgreSQL >= 15 already denies PUBLIC CREATE on the public schema.
    public_schema_locked_by_default: bool = False


async def apply_schema_privileges(
    conn: AsyncConnection,
    schema: str,
    roles: GroupRoles,
    options: GrantOptions,
) -> None:
    """Grant schema and object privileges for all three group roles.

    Runs against the target database. Safe to re-run: PostgreSQL treats a
    duplicate GRANT as a no-op.
    """
    if options.revoke_public_schema_create and not (
        schema == "public" and options.public_schema_locked_by_default
    ):
        # PUBLIC holds CREATE on `public` before PostgreSQL 15; leaving it means
        # any role that can connect can create objects outside the model.
        await execute(
            conn,
            sql.SQL("REVOKE CREATE ON SCHEMA {} FROM PUBLIC").format(ident(schema)),
        )

    await execute(
        conn,
        sql.SQL("GRANT ALL ON SCHEMA {} TO {}").format(ident(schema), ident(roles.owner)),
    )
    await execute(
        conn,
        sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
            ident(schema), idents(roles.readers)
        ),
    )

    for level_key, role in (("readWrite", roles.read_write), ("readOnly", roles.read_only)):
        await _grant_existing_objects(conn, schema, role, level_key, options)


async def _grant_existing_objects(
    conn: AsyncConnection,
    schema: str,
    role: str,
    level_key: str,
    options: GrantOptions,
) -> None:
    await execute(
        conn,
        sql.SQL("GRANT {} ON ALL TABLES IN SCHEMA {} TO {}").format(
            _privilege_list(TABLE_PRIVILEGES[level_key]), ident(schema), ident(role)
        ),
    )
    await execute(
        conn,
        sql.SQL("GRANT {} ON ALL SEQUENCES IN SCHEMA {} TO {}").format(
            _privilege_list(SEQUENCE_PRIVILEGES[level_key]), ident(schema), ident(role)
        ),
    )
    if level_key == "readWrite" or options.grant_execute_to_read_only:
        await execute(
            conn,
            sql.SQL("GRANT {} ON ALL FUNCTIONS IN SCHEMA {} TO {}").format(
                _privilege_list(ROUTINE_PRIVILEGES[level_key]), ident(schema), ident(role)
            ),
        )


async def apply_default_privileges(
    conn: AsyncConnection,
    schema: str,
    roles: GroupRoles,
    options: GrantOptions,
    *,
    creators: list[str] | None = None,
) -> None:
    """Set DEFAULT PRIVILEGES so future objects are reachable without re-granting.

    ``creators`` defaults to the owner group alone. Additional creator roles are
    passed when ``setRoleForOwners`` is disabled and owner-group members
    therefore create objects under their own login role.
    """
    for creator in creators or [roles.owner]:
        for level_key, role in (
            ("readWrite", roles.read_write),
            ("readOnly", roles.read_only),
        ):
            await _default_privileges_for(conn, schema, creator, role, level_key, options)


async def _default_privileges_for(
    conn: AsyncConnection,
    schema: str,
    creator: str,
    role: str,
    level_key: str,
    options: GrantOptions,
) -> None:
    prefix = sql.SQL("ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA {}").format(
        ident(creator), ident(schema)
    )
    await execute(
        conn,
        sql.SQL("{} GRANT {} ON TABLES TO {}").format(
            prefix, _privilege_list(TABLE_PRIVILEGES[level_key]), ident(role)
        ),
    )
    await execute(
        conn,
        sql.SQL("{} GRANT {} ON SEQUENCES TO {}").format(
            prefix, _privilege_list(SEQUENCE_PRIVILEGES[level_key]), ident(role)
        ),
    )
    if level_key == "readWrite" or options.grant_execute_to_read_only:
        await execute(
            conn,
            sql.SQL("{} GRANT {} ON FUNCTIONS TO {}").format(
                prefix, _privilege_list(ROUTINE_PRIVILEGES[level_key]), ident(role)
            ),
        )
    await execute(
        conn,
        sql.SQL("{} GRANT {} ON TYPES TO {}").format(
            prefix, _privilege_list(TYPE_PRIVILEGES[level_key]), ident(role)
        ),
    )


async def observed_schema_grants(
    conn: AsyncConnection, roles: GroupRoles
) -> dict[str, list[str]]:
    """Schema-level privileges actually held by each group role.

    Reported on status so the effective model is visible without a psql session.
    """
    rows = await fetch_all(
        conn,
        sql.SQL(
            """
            SELECT n.nspname, r.rolname,
                   array_remove(ARRAY[
                     CASE WHEN has_schema_privilege(r.rolname, n.nspname, 'USAGE')
                          THEN 'USAGE' END,
                     CASE WHEN has_schema_privilege(r.rolname, n.nspname, 'CREATE')
                          THEN 'CREATE' END
                   ], NULL)
              FROM pg_namespace n
              CROSS JOIN pg_roles r
             WHERE n.nspname NOT LIKE 'pg\\_%%'
               AND n.nspname <> 'information_schema'
               AND r.rolname = ANY(%s)
             ORDER BY n.nspname, r.rolname
            """
        ),
        (roles.all,),
    )
    grants: dict[str, list[str]] = {}
    for schema_name, role_name, privileges in rows:
        if privileges:
            grants.setdefault(str(schema_name), []).append(
                f"{role_name}:{'+'.join(privileges)}"
            )
    return grants


def _privilege_list(privileges: tuple[str, ...]) -> sql.Composable:
    """Render a privilege keyword list. Values come from the tables above only."""
    return sql.SQL(", ").join(sql.SQL(p) for p in privileges)
