"""Server-level introspection: version, current identity, effective privileges."""

from __future__ import annotations

from dataclasses import dataclass

from psycopg import AsyncConnection, sql

from .sql import fetch_one, fetch_scalar


@dataclass(frozen=True, slots=True)
class ServerInfo:
    """What the operator learned about an instance on connect."""

    version_num: int
    version_text: str
    current_user: str
    is_superuser: bool
    can_create_db: bool
    can_create_role: bool
    is_rds_superuser: bool

    @property
    def major_version(self) -> int:
        return self.version_num // 10_000

    @property
    def version(self) -> str:
        """Short server version, e.g. ``16.3``."""
        major = self.version_num // 10_000
        minor = self.version_num % 10_000
        return f"{major}.{minor}"

    @property
    def supports_drop_database_force(self) -> bool:
        """``DROP DATABASE ... WITH (FORCE)`` landed in PostgreSQL 13."""
        return self.major_version >= 13

    @property
    def public_schema_is_locked_down(self) -> bool:
        """PostgreSQL 15 removed PUBLIC's CREATE privilege on the public schema."""
        return self.major_version >= 15

    @property
    def supports_per_grant_options(self) -> bool:
        """PostgreSQL 16 moved INHERIT and SET onto the individual role grant.

        It also stopped granting them implicitly: a CREATEROLE role that creates
        a role gets ADMIN OPTION but ``INHERIT FALSE, SET FALSE``, so the
        operator has to ask for them explicitly.
        """
        return self.major_version >= 16

    def describe_privileges(self) -> str:
        held = [
            name
            for name, ok in (
                ("SUPERUSER", self.is_superuser),
                ("CREATEDB", self.can_create_db),
                ("CREATEROLE", self.can_create_role),
                ("rds_superuser", self.is_rds_superuser),
            )
            if ok
        ]
        return ", ".join(held) if held else "none"


_INFO_QUERY = sql.SQL(
    """
    SELECT current_setting('server_version_num')::int AS version_num,
           current_setting('server_version')          AS version_text,
           current_user                               AS current_user,
           r.rolsuper                                 AS is_superuser,
           r.rolcreatedb                              AS can_create_db,
           r.rolcreaterole                            AS can_create_role,
           pg_has_role(current_user, 'rds_superuser', 'USAGE') AS is_rds_superuser
      FROM pg_roles r
     WHERE r.rolname = current_user
    """
)

# `rds_superuser` only exists on RDS/Aurora; probing for it on vanilla
# PostgreSQL raises undefined_object, so fall back to a version without it.
_INFO_QUERY_NO_RDS = sql.SQL(
    """
    SELECT current_setting('server_version_num')::int,
           current_setting('server_version'),
           current_user,
           r.rolsuper,
           r.rolcreatedb,
           r.rolcreaterole,
           false
      FROM pg_roles r
     WHERE r.rolname = current_user
    """
)


async def describe_server(conn: AsyncConnection) -> ServerInfo:
    """Read version and the managing role's effective privileges."""
    has_rds_role = await fetch_scalar(
        conn,
        sql.SQL("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rds_superuser')"),
    )
    row = await fetch_one(conn, _INFO_QUERY if has_rds_role else _INFO_QUERY_NO_RDS)
    if row is None:  # pragma: no cover - current_user always exists in pg_roles
        raise RuntimeError("could not resolve current_user in pg_roles")
    return ServerInfo(
        version_num=int(row[0]),
        version_text=str(row[1]),
        current_user=str(row[2]),
        is_superuser=bool(row[3]),
        can_create_db=bool(row[4]),
        can_create_role=bool(row[5]),
        is_rds_superuser=bool(row[6]),
    )
