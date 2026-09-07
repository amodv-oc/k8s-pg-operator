"""SQL execution helpers.

Every identifier reaching the server goes through ``psycopg.sql.Identifier`` and
every value through either a bound parameter or ``psycopg.sql.Literal``; no
statement in this package is built by string interpolation.
"""

from __future__ import annotations

import logging
from typing import Any

from psycopg import AsyncConnection, errors, sql

from ..errors import InsufficientPrivilege, ObjectMissing, ReconcileFailure

log = logging.getLogger(__name__)

Statement = sql.Composed | sql.SQL


def ident(name: str) -> sql.Identifier:
    """Quote a single identifier."""
    return sql.Identifier(name)


def idents(names: list[str] | tuple[str, ...]) -> sql.Composed:
    """Comma-separated list of quoted identifiers, e.g. for a GRANT target list."""
    return sql.SQL(", ").join(sql.Identifier(name) for name in names)


def qualified(schema: str, name: str) -> sql.Identifier:
    """A schema-qualified identifier."""
    return sql.Identifier(schema, name)


def literal(value: Any) -> sql.Literal:
    """Quote a value for contexts that forbid bound parameters (e.g. passwords)."""
    return sql.Literal(value)


async def execute(
    conn: AsyncConnection,
    statement: Statement,
    params: Any = None,
    *,
    describe: str | None = None,
) -> None:
    """Run a statement, translating driver errors into ``ReconcileFailure``."""
    rendered = describe or _render(statement, conn)
    log.debug("executing: %s", rendered)
    try:
        async with conn.cursor() as cur:
            await cur.execute(statement, params)
    except errors.InsufficientPrivilege as exc:
        raise InsufficientPrivilege(
            f"insufficient privilege for `{rendered}`: {_terse(exc)}. "
            "The credentials in the instance's Secret must be a superuser or, "
            "on RDS, a member of rds_superuser with CREATEDB and CREATEROLE."
        ) from exc
    except (errors.InvalidCatalogName, errors.UndefinedObject) as exc:
        raise ObjectMissing(f"`{rendered}` failed: {_terse(exc)}") from exc
    except errors.Error as exc:
        raise ReconcileFailure(f"`{rendered}` failed: {_terse(exc)}") from exc


async def fetch_all(
    conn: AsyncConnection, statement: Statement, params: Any = None
) -> list[tuple[Any, ...]]:
    try:
        async with conn.cursor() as cur:
            await cur.execute(statement, params)
            return await cur.fetchall()
    except errors.Error as exc:
        raise ReconcileFailure(f"query failed: {_terse(exc)}") from exc


async def fetch_one(
    conn: AsyncConnection, statement: Statement, params: Any = None
) -> tuple[Any, ...] | None:
    try:
        async with conn.cursor() as cur:
            await cur.execute(statement, params)
            return await cur.fetchone()
    except errors.Error as exc:
        raise ReconcileFailure(f"query failed: {_terse(exc)}") from exc


async def fetch_scalar(
    conn: AsyncConnection, statement: Statement, params: Any = None
) -> Any:
    row = await fetch_one(conn, statement, params)
    return row[0] if row else None


async def fetch_column(
    conn: AsyncConnection, statement: Statement, params: Any = None
) -> list[Any]:
    return [row[0] for row in await fetch_all(conn, statement, params)]


def _render(statement: Statement, conn: AsyncConnection) -> str:
    try:
        return statement.as_string(conn)
    except Exception:  # noqa: BLE001 - only used for log/error text
        return "<unrenderable statement>"


def _terse(exc: BaseException) -> str:
    text = str(exc).strip()
    return text.splitlines()[0] if text else exc.__class__.__name__
