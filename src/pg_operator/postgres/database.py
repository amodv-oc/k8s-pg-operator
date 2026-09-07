"""Database-level operations: creation, attributes, schemas, extensions, teardown."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import StrEnum

from psycopg import AsyncConnection, sql

from ..errors import InsufficientPrivilege, ReconcileFailure
from .settings import parse_setconfig
from .sql import (
    execute,
    fetch_all,
    fetch_column,
    fetch_one,
    fetch_scalar,
    ident,
    idents,
    literal,
)

log = logging.getLogger(__name__)

@dataclass(frozen=True, slots=True)
class DatabaseState:
    """Observed attributes of an existing database."""

    name: str
    owner: str
    encoding: str
    connection_limit: int
    collate: str | None = None
    ctype: str | None = None


@dataclass(slots=True)
class SchemaState:
    name: str
    owner: str


@dataclass(slots=True)
class ExtensionState:
    name: str
    version: str
    schema: str


@dataclass(slots=True)
class DatabaseObservation:
    """Snapshot of a database used for status reporting and drift detection."""

    schemas: list[SchemaState] = field(default_factory=list)
    extensions: list[ExtensionState] = field(default_factory=list)
    parameters: dict[str, str] = field(default_factory=dict)

    @property
    def schema_names(self) -> set[str]:
        return {s.name for s in self.schemas}

    @property
    def extension_names(self) -> set[str]:
        return {e.name for e in self.extensions}


# ---------------------------------------------------------------------------
# database lifecycle
# ---------------------------------------------------------------------------


async def get_database(conn: AsyncConnection, name: str) -> DatabaseState | None:
    row = await fetch_one(
        conn,
        sql.SQL(
            """
            SELECT d.datname,
                   r.rolname,
                   pg_encoding_to_char(d.encoding),
                   d.datconnlimit,
                   d.datcollate,
                   d.datctype
              FROM pg_database d
              JOIN pg_roles r ON r.oid = d.datdba
             WHERE d.datname = %s
            """
        ),
        (name,),
    )
    if row is None:
        return None
    return DatabaseState(
        name=str(row[0]),
        owner=str(row[1]),
        encoding=str(row[2]),
        connection_limit=int(row[3]),
        collate=row[4],
        ctype=row[5],
    )


async def create_database(
    conn: AsyncConnection,
    name: str,
    *,
    owner: str,
    encoding: str = "UTF8",
    template: str = "template0",
    lc_collate: str | None = None,
    lc_ctype: str | None = None,
    connection_limit: int = -1,
) -> None:
    """``CREATE DATABASE``. Must run outside a transaction block.

    Locale and encoding are fixed at creation; PostgreSQL offers no ALTER for
    them, so a later spec change is surfaced as immutable-field drift rather
    than silently ignored.
    """
    options: list[sql.Composable] = [
        sql.SQL("OWNER = {}").format(ident(owner)),
        sql.SQL("ENCODING = {}").format(literal(encoding)),
        sql.SQL("TEMPLATE = {}").format(ident(template)),
        sql.SQL("CONNECTION LIMIT = {}").format(literal(connection_limit)),
    ]
    if lc_collate:
        options.append(sql.SQL("LC_COLLATE = {}").format(literal(lc_collate)))
    if lc_ctype:
        options.append(sql.SQL("LC_CTYPE = {}").format(literal(lc_ctype)))

    await execute(
        conn,
        sql.SQL("CREATE DATABASE {} WITH {}").format(
            ident(name), sql.SQL(" ").join(options)
        ),
    )
    log.info("created database %s owned by %s", name, owner)


async def set_database_owner(conn: AsyncConnection, name: str, owner: str) -> None:
    await execute(
        conn, sql.SQL("ALTER DATABASE {} OWNER TO {}").format(ident(name), ident(owner))
    )


async def set_database_connection_limit(
    conn: AsyncConnection, name: str, limit: int
) -> None:
    await execute(
        conn,
        sql.SQL("ALTER DATABASE {} WITH CONNECTION LIMIT {}").format(
            ident(name), literal(limit)
        ),
    )


async def comment_on_database(conn: AsyncConnection, name: str, comment: str | None) -> None:
    await execute(
        conn,
        sql.SQL("COMMENT ON DATABASE {} IS {}").format(
            ident(name), literal(comment) if comment else sql.SQL("NULL")
        ),
    )


async def get_database_parameters(conn: AsyncConnection, name: str) -> dict[str, str]:
    """Database-wide settings from ``pg_db_role_setting`` (setrole = 0)."""
    rows = await fetch_column(
        conn,
        sql.SQL(
            """
            SELECT s.setconfig
              FROM pg_db_role_setting s
              JOIN pg_database d ON d.oid = s.setdatabase
             WHERE d.datname = %s AND s.setrole = 0
            """
        ),
        (name,),
    )
    settings: dict[str, str] = {}
    for config in rows:
        settings.update(parse_setconfig(config))
    return settings


@dataclass(slots=True)
class ParameterOutcome:
    """Result of converging a database's parameters.

    ``denied`` holds the parameters PostgreSQL refused. Many GUCs are
    superuser-only (``log_min_duration_statement`` among them), and on RDS the
    master user can set them only because RDS grants it that right. A refusal
    is reported rather than raised so that the schemas, extensions and grants
    that make up the rest of the database still converge: the alternative is a
    single unsettable parameter permanently blocking every other object.
    """

    changes: list[str] = field(default_factory=list)
    denied: list[str] = field(default_factory=list)


async def apply_database_parameters(
    conn: AsyncConnection,
    name: str,
    desired: dict[str, str],
    observed: dict[str, str],
) -> ParameterOutcome:
    """Converge ``ALTER DATABASE ... SET``; resets settings no longer declared."""
    outcome = ParameterOutcome()
    for key, value in desired.items():
        if observed.get(key) == value:
            continue
        try:
            await execute(
                conn,
                sql.SQL("ALTER DATABASE {} SET {} TO {}").format(
                    ident(name), ident(key), literal(value)
                ),
            )
        except InsufficientPrivilege as exc:
            log.warning("cannot set %s on database %s: %s", key, name, exc)
            outcome.denied.append(
                f"parameter {key} could not be set: the managing role may not "
                f"change it ({key} is superuser-only on a stock server; on RDS "
                "it requires the rds_superuser grant)"
            )
            continue
        outcome.changes.append(f"set {key}={value}")
    for key in observed.keys() - desired.keys():
        try:
            await execute(
                conn,
                sql.SQL("ALTER DATABASE {} RESET {}").format(ident(name), ident(key)),
            )
        except InsufficientPrivilege as exc:
            log.warning("cannot reset %s on database %s: %s", key, name, exc)
            outcome.denied.append(
                f"parameter {key} could not be reset: the managing role may not "
                "change it"
            )
            continue
        outcome.changes.append(f"reset {key}")
    return outcome


async def connection_roles(conn: AsyncConnection, name: str) -> list[str]:
    """Distinct roles holding a session on ``name``, excluding this one."""
    rows = await fetch_column(
        conn,
        sql.SQL(
            "SELECT DISTINCT usename FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid() AND usename IS NOT NULL"
        ),
        (name,),
    )
    return sorted(str(row) for row in rows)


async def terminate_connections(
    conn: AsyncConnection, name: str
) -> tuple[int, list[str]]:
    """Terminate sessions on ``name``. Returns ``(terminated, unsignalled roles)``.

    ``pg_terminate_backend`` is only permitted for a backend whose role the
    caller is a member of, or with ``pg_signal_backend``. An RDS master user has
    neither for an application's own login roles, so backends are terminated one
    at a time and a refusal is reported instead of aborting the teardown: a
    single un-signallable session must not wedge the finalizer.
    """
    backends = await fetch_all(
        conn,
        sql.SQL(
            "SELECT pid, coalesce(usename, '?') FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid()"
        ),
        (name,),
    )
    terminated = 0
    unsignalled: set[str] = set()
    for pid, usename in backends:
        try:
            await fetch_scalar(
                conn, sql.SQL("SELECT pg_terminate_backend(%s)"), (int(pid),)
            )
            terminated += 1
        except ReconcileFailure as exc:
            # The backend may also have exited on its own between the two
            # statements, which is a success for our purposes.
            if "does not exist" in str(exc):
                continue
            unsignalled.add(str(usename))
    if unsignalled:
        log.warning(
            "could not terminate session(s) on %s owned by %s; the managing role "
            "is not a member of those roles and lacks pg_signal_backend",
            name,
            ", ".join(sorted(unsignalled)),
        )
    return terminated, sorted(unsignalled)


async def set_allow_connections(conn: AsyncConnection, name: str, allow: bool) -> None:
    """Flip ``datallowconn`` so new sessions cannot race a pending DROP."""
    await execute(
        conn,
        sql.SQL("ALTER DATABASE {} WITH ALLOW_CONNECTIONS {}").format(
            ident(name), sql.SQL("true" if allow else "false")
        ),
    )


async def drop_database(conn: AsyncConnection, name: str, *, force: bool) -> None:
    """``DROP DATABASE``. Must run outside a transaction block."""
    statement = sql.SQL("DROP DATABASE IF EXISTS {}").format(ident(name))
    if force:
        statement = sql.SQL("{} WITH (FORCE)").format(statement)
    await execute(conn, statement)
    log.info("dropped database %s", name)


# ---------------------------------------------------------------------------
# database-level privileges (run against the maintenance database)
# ---------------------------------------------------------------------------


async def revoke_public_database_privileges(conn: AsyncConnection, name: str) -> None:
    """Take CONNECT/TEMP away from PUBLIC so only the group roles can attach."""
    await execute(
        conn, sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(ident(name))
    )


async def grant_database_connect(
    conn: AsyncConnection, name: str, roles: list[str], *, with_temp: bool = False
) -> None:
    privileges = sql.SQL("CONNECT, TEMPORARY") if with_temp else sql.SQL("CONNECT")
    await execute(
        conn,
        sql.SQL("GRANT {} ON DATABASE {} TO {}").format(
            privileges, ident(name), idents(roles)
        ),
    )


# ---------------------------------------------------------------------------
# schemas (run against the target database)
# ---------------------------------------------------------------------------


async def list_schemas(conn: AsyncConnection) -> list[SchemaState]:
    """User-visible schemas, excluding system and per-session temp schemas."""
    rows = await fetch_all(
        conn,
        sql.SQL(
            """
            SELECT n.nspname, r.rolname
              FROM pg_namespace n
              JOIN pg_roles r ON r.oid = n.nspowner
             WHERE n.nspname NOT LIKE 'pg\\_%%'
               AND n.nspname <> 'information_schema'
             ORDER BY n.nspname
            """
        ),
    )
    return [SchemaState(name=str(row[0]), owner=str(row[1])) for row in rows]


class SchemaOutcome(StrEnum):
    """What ``ensure_schema`` was able to do."""

    CREATED = "created"
    OWNER_CHANGED = "owner-changed"
    UNCHANGED = "unchanged"
    #: The schema exists but its owner could not be changed. On PostgreSQL
    #: before 15 the ``public`` schema belongs to the bootstrap superuser, and
    #: a non-superuser managing role (an RDS master user) cannot reassign it.
    OWNER_DENIED = "owner-denied"


async def ensure_schema(
    conn: AsyncConnection, name: str, owner: str
) -> SchemaOutcome:
    """Create the schema if absent and hand ownership to ``owner``.

    A refused ownership change is reported rather than raised: it affects one
    schema, and failing the whole database over ``public`` would be
    disproportionate when every other schema is manageable.
    """
    existing = await fetch_one(
        conn,
        sql.SQL(
            "SELECT r.rolname FROM pg_namespace n JOIN pg_roles r ON r.oid = n.nspowner "
            "WHERE n.nspname = %s"
        ),
        (name,),
    )
    if existing is None:
        await execute(
            conn,
            sql.SQL("CREATE SCHEMA {} AUTHORIZATION {}").format(ident(name), ident(owner)),
        )
        log.info("created schema %s owned by %s", name, owner)
        return SchemaOutcome.CREATED

    current_owner = str(existing[0])
    if current_owner == owner:
        return SchemaOutcome.UNCHANGED

    # pg_database_owner (PostgreSQL 15+) resolves to whoever owns the database,
    # which is already the owner group, so no reassignment is needed.
    if current_owner == "pg_database_owner":
        return SchemaOutcome.UNCHANGED

    try:
        await execute(
            conn,
            sql.SQL("ALTER SCHEMA {} OWNER TO {}").format(ident(name), ident(owner)),
        )
    except InsufficientPrivilege:
        log.warning(
            "cannot change owner of schema %s from %s to %s; the managing role "
            "is neither a superuser nor the schema's owner",
            name,
            current_owner,
            owner,
        )
        return SchemaOutcome.OWNER_DENIED
    log.info("reassigned schema %s to %s", name, owner)
    return SchemaOutcome.OWNER_CHANGED


async def comment_on_schema(conn: AsyncConnection, name: str, comment: str | None) -> None:
    await execute(
        conn,
        sql.SQL("COMMENT ON SCHEMA {} IS {}").format(
            ident(name), literal(comment) if comment else sql.SQL("NULL")
        ),
    )


async def drop_schema(conn: AsyncConnection, name: str, *, cascade: bool) -> None:
    statement = sql.SQL("DROP SCHEMA IF EXISTS {}").format(ident(name))
    statement = sql.SQL("{} {}").format(
        statement, sql.SQL("CASCADE" if cascade else "RESTRICT")
    )
    await execute(conn, statement)
    log.info("dropped schema %s (cascade=%s)", name, cascade)


async def schema_is_empty(conn: AsyncConnection, name: str) -> bool:
    """Whether a schema holds no relations — used to keep RESTRICT drops safe."""
    count = await fetch_scalar(
        conn, sql.SQL("SELECT count(*) FROM pg_class c JOIN pg_namespace n "
                      "ON n.oid = c.relnamespace WHERE n.nspname = %s"), (name,)
    )
    return not int(count or 0)


# ---------------------------------------------------------------------------
# extensions (run against the target database)
# ---------------------------------------------------------------------------


async def list_extensions(conn: AsyncConnection) -> list[ExtensionState]:
    rows = await fetch_all(
        conn,
        sql.SQL(
            """
            SELECT e.extname, e.extversion, n.nspname
              FROM pg_extension e
              JOIN pg_namespace n ON n.oid = e.extnamespace
             ORDER BY e.extname
            """
        ),
    )
    return [
        ExtensionState(name=str(row[0]), version=str(row[1]), schema=str(row[2]))
        for row in rows
    ]


async def available_extensions(conn: AsyncConnection) -> set[str]:
    return set(
        await fetch_column(conn, sql.SQL("SELECT name FROM pg_available_extensions"))
    )


async def create_extension(
    conn: AsyncConnection,
    name: str,
    *,
    schema: str | None = None,
    version: str | None = None,
    cascade: bool = False,
) -> None:
    options: list[sql.Composable] = []
    if schema:
        options.append(sql.SQL("SCHEMA {}").format(ident(schema)))
    if version:
        options.append(sql.SQL("VERSION {}").format(literal(version)))
    if cascade:
        options.append(sql.SQL("CASCADE"))
    statement = sql.SQL("CREATE EXTENSION IF NOT EXISTS {}").format(ident(name))
    if options:
        statement = sql.SQL("{} WITH {}").format(statement, sql.SQL(" ").join(options))
    await execute(conn, statement)
    log.info("created extension %s", name)


async def update_extension(conn: AsyncConnection, name: str, version: str | None) -> None:
    statement = sql.SQL("ALTER EXTENSION {} UPDATE").format(ident(name))
    if version:
        statement = sql.SQL("{} TO {}").format(statement, literal(version))
    await execute(conn, statement)


async def drop_extension(conn: AsyncConnection, name: str, *, cascade: bool) -> None:
    statement = sql.SQL("DROP EXTENSION IF EXISTS {}").format(ident(name))
    if cascade:
        statement = sql.SQL("{} CASCADE").format(statement)
    await execute(conn, statement)
    log.info("dropped extension %s", name)


async def observe(conn: AsyncConnection, name: str) -> DatabaseObservation:
    """Read the live state of a database for status and drift reporting."""
    return DatabaseObservation(
        schemas=await list_schemas(conn),
        extensions=await list_extensions(conn),
        parameters=await get_database_parameters(conn, name),
    )
