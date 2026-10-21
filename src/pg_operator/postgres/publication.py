"""Publications, and the catalog facts a table is judged on before joining one.

The operator lists a publication's tables explicitly (design D1) and adds one
only once both servers can take it. This module reads what that judgement needs
- from the source: kind, owner, replica identity, columns, row security, the
replication login's SELECT; from the target: whether the table and its columns
exist and whose they are - and applies the publication DDL. Deciding which
tables qualify is :mod:`pg_operator.reconcile.eligibility`'s job, not this one's.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from psycopg import AsyncConnection, sql

from .sql import execute, fetch_all, fetch_one, fetch_scalar, ident, idents, literal, qualified

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SourceTable:
    """A relation in the source database, as a publication would see it."""

    oid: int
    schema: str
    name: str
    #: pg_class.relkind: 'r' table, 'p' partitioned table, anything else unpublishable.
    kind: str
    persistent: bool
    is_partition: bool
    owner: str
    #: The managing role holds the owner's privileges, which ADD TABLE requires.
    owner_usable: bool
    row_security: bool
    #: pg_class.relreplident: 'd' default (primary key), 'i' index, 'f' full, 'n' nothing.
    replica_identity: str
    #: Columns the replica identity covers, or None when there is no usable one.
    identity_columns: frozenset[str] | None
    #: Publishable columns (not generated, not dropped) and their types, in order.
    columns: dict[str, str]
    generated: frozenset[str]
    readable_by_login: bool
    #: Leaf partitions of a partitioned table; empty otherwise.
    leaves: tuple[SourceTable, ...] = ()

    @property
    def qualified(self) -> str:
        return f"{self.schema}.{self.name}"


@dataclass(frozen=True, slots=True)
class TargetTable:
    """A relation in the target database that a subscription would apply into."""

    schema: str
    name: str
    kind: str
    owner: str
    #: The managing role may SET ROLE to the owner, which PostgreSQL 16's
    #: apply-as-table-owner needs.
    owner_settable: bool
    columns: dict[str, str]
    generated: frozenset[str]

    @property
    def qualified(self) -> str:
        return f"{self.schema}.{self.name}"


@dataclass(frozen=True, slots=True)
class PublishedTable:
    """A table's membership of a publication, as the server holds it."""

    #: None publishes every column.
    columns: frozenset[str] | None
    #: The server's own deparse of the row filter, or None.
    row_filter: str | None


@dataclass(frozen=True, slots=True)
class PublicationState:
    name: str
    owner: str
    comment: str | None
    operations: frozenset[str]
    via_root: bool
    tables: dict[str, PublishedTable] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# source catalog
# ---------------------------------------------------------------------------

_RELATIONS = """
    SELECT c.oid, n.nspname, c.relname, c.relkind, c.relpersistence = 'p',
           c.relispartition, pg_get_userbyid(c.relowner),
           pg_has_role(c.relowner, 'USAGE'), c.relrowsecurity, c.relreplident,
           has_table_privilege(%(login)s::text, c.oid, 'SELECT')
      FROM pg_class c
      JOIN pg_namespace n ON n.oid = c.relnamespace
"""

_IN_SCHEMAS = sql.SQL(
    _RELATIONS
    + """
     WHERE n.nspname = ANY(%(schemas)s)
       AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
    """
)

_BY_NAME = sql.SQL(
    _RELATIONS
    + """
      JOIN unnest(%(schemas)s::text[], %(names)s::text[]) AS w(schema, name)
        ON w.schema = n.nspname AND w.name = c.relname
    """
)

_BY_OID = sql.SQL(_RELATIONS + " WHERE c.oid = ANY(%(oids)s::oid[])")

_COLUMNS = sql.SQL(
    """
    SELECT a.attrelid, a.attname, format_type(a.atttypid, a.atttypmod),
           a.attgenerated <> ''
      FROM pg_attribute a
     WHERE a.attrelid = ANY(%s::oid[]) AND a.attnum > 0 AND NOT a.attisdropped
     ORDER BY a.attrelid, a.attnum
    """
)

#: The index a replica identity uses: the primary key by default, or the index
#: chosen with REPLICA IDENTITY USING INDEX.
_IDENTITY_COLUMNS = sql.SQL(
    """
    SELECT i.indrelid, a.attname
      FROM pg_index i
      JOIN pg_class c ON c.oid = i.indrelid
      JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
     WHERE i.indrelid = ANY(%s::oid[])
       AND ((c.relreplident = 'd' AND i.indisprimary)
         OR (c.relreplident = 'i' AND i.indisreplident))
    """
)

_LEAVES = sql.SQL(
    "SELECT relid::oid FROM pg_partition_tree(%s::oid::regclass) "
    "WHERE isleaf AND relid <> %s::oid::regclass"
)


async def describe_source_tables(
    conn: AsyncConnection,
    *,
    schemas: Iterable[str],
    tables: Iterable[tuple[str, str]],
    login: str,
) -> dict[str, SourceTable]:
    """Every relation in ``schemas``, plus each named table, keyed by ``schema.name``.

    Partitions are included here (an explicit entry may name one by mistake)
    and leaves are attached to their partitioned table; choosing what to
    publish is left to the caller.
    """
    rows = await fetch_all(conn, _IN_SCHEMAS, {"login": login, "schemas": sorted(set(schemas))})
    named = sorted(set(tables))
    if named:
        rows += await fetch_all(
            conn,
            _BY_NAME,
            {"login": login, "schemas": [s for s, _ in named], "names": [n for _, n in named]},
        )
    found = await _assemble(conn, rows, login)
    return {table.qualified: table for table in found}


async def _assemble(
    conn: AsyncConnection, rows: Sequence[tuple[Any, ...]], login: str
) -> list[SourceTable]:
    by_oid = {int(row[0]): row for row in rows}
    if not by_oid:
        return []
    columns: dict[int, list[tuple[str, str, bool]]] = {}
    for relid, name, type_, generated in await fetch_all(conn, _COLUMNS, (list(by_oid),)):
        columns.setdefault(int(relid), []).append((str(name), str(type_), bool(generated)))
    identity: dict[int, set[str]] = {}
    for relid, name in await fetch_all(conn, _IDENTITY_COLUMNS, (list(by_oid),)):
        identity.setdefault(int(relid), set()).add(str(name))

    assembled: list[SourceTable] = []
    for oid, row in by_oid.items():
        leaves: tuple[SourceTable, ...] = ()
        if row[3] == "p":
            leaf_oids = [
                int(r[0]) for r in await fetch_all(conn, _LEAVES, (oid, oid))
            ]
            if leaf_oids:
                leaf_rows = await fetch_all(conn, _BY_OID, {"login": login, "oids": leaf_oids})
                leaves = tuple(await _assemble(conn, leaf_rows, login))
        assembled.append(_source_table(row, columns.get(oid, []), identity.get(oid), leaves))
    return assembled


def _source_table(
    row: tuple[Any, ...],
    columns: list[tuple[str, str, bool]],
    identity: set[str] | None,
    leaves: tuple[SourceTable, ...],
) -> SourceTable:
    publishable = {name: type_ for name, type_, generated in columns if not generated}
    replica_identity = str(row[9])
    identity_columns: frozenset[str] | None
    if replica_identity == "f":
        # FULL: every published column identifies the row.
        identity_columns = frozenset(publishable)
    elif replica_identity in {"d", "i"} and identity:
        identity_columns = frozenset(identity)
    else:
        identity_columns = None
    return SourceTable(
        oid=int(row[0]),
        schema=str(row[1]),
        name=str(row[2]),
        kind=str(row[3]),
        persistent=bool(row[4]),
        is_partition=bool(row[5]),
        owner=str(row[6]),
        owner_usable=bool(row[7]),
        row_security=bool(row[8]),
        replica_identity=replica_identity,
        identity_columns=identity_columns,
        columns=publishable,
        generated=frozenset(name for name, _, generated in columns if generated),
        readable_by_login=bool(row[10]),
        leaves=leaves,
    )


# ---------------------------------------------------------------------------
# target catalog
# ---------------------------------------------------------------------------

_TARGET_RELATIONS = sql.SQL(
    """
    SELECT c.oid, n.nspname, c.relname, c.relkind, pg_get_userbyid(c.relowner),
           pg_has_role(c.relowner, 'SET')
      FROM pg_class c
      JOIN pg_namespace n ON n.oid = c.relnamespace
      JOIN unnest(%s::text[], %s::text[]) AS w(schema, name)
        ON w.schema = n.nspname AND w.name = c.relname
    """
)


async def describe_target_tables(
    conn: AsyncConnection, wanted: Iterable[tuple[str, str]]
) -> dict[str, TargetTable]:
    """The named relations that exist in the target database, keyed by ``schema.name``."""
    names = sorted(set(wanted))
    if not names:
        return {}
    rows = await fetch_all(
        conn, _TARGET_RELATIONS, ([s for s, _ in names], [n for _, n in names])
    )
    oids = [int(row[0]) for row in rows]
    columns: dict[int, list[tuple[str, str, bool]]] = {}
    for relid, name, type_, generated in await fetch_all(conn, _COLUMNS, (oids,)):
        columns.setdefault(int(relid), []).append((str(name), str(type_), bool(generated)))
    found: dict[str, TargetTable] = {}
    for row in rows:
        oid = int(row[0])
        table = TargetTable(
            schema=str(row[1]),
            name=str(row[2]),
            kind=str(row[3]),
            owner=str(row[4]),
            owner_settable=bool(row[5]),
            columns={n: t for n, t, generated in columns.get(oid, []) if not generated},
            generated=frozenset(n for n, _, generated in columns.get(oid, []) if generated),
        )
        found[table.qualified] = table
    return found


# ---------------------------------------------------------------------------
# publication
# ---------------------------------------------------------------------------

_PUBLICATION = sql.SQL(
    """
    SELECT p.oid, pg_get_userbyid(p.pubowner), obj_description(p.oid, 'pg_publication'),
           p.pubinsert, p.pubupdate, p.pubdelete, p.pubtruncate, p.pubviaroot
      FROM pg_publication p
     WHERE p.pubname = %s
    """
)

_PUBLICATION_TABLES = sql.SQL(
    """
    SELECT n.nspname, c.relname, pg_get_expr(pr.prqual, pr.prrelid),
           (SELECT array_agg(a.attname ORDER BY a.attnum)
              FROM pg_attribute a
             WHERE a.attrelid = pr.prrelid AND a.attnum = ANY(pr.prattrs::int2[]))
      FROM pg_publication_rel pr
      JOIN pg_class c ON c.oid = pr.prrelid
      JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE pr.prpubid = %s
    """
)


async def get_publication(conn: AsyncConnection, name: str) -> PublicationState | None:
    row = await fetch_one(conn, _PUBLICATION, (name,))
    if row is None:
        return None
    operations = frozenset(
        op
        for op, enabled in zip(("insert", "update", "delete", "truncate"), row[3:7], strict=True)
        if enabled
    )
    tables = {
        f"{schema}.{table}": PublishedTable(
            columns=frozenset(columns) if columns is not None else None,
            row_filter=row_filter,
        )
        for schema, table, row_filter, columns in await fetch_all(conn, _PUBLICATION_TABLES, (row[0],))
    }
    return PublicationState(
        name=name,
        owner=str(row[1]),
        comment=row[2],
        operations=operations,
        via_root=bool(row[7]),
        tables=tables,
    )


def _options(operations: Sequence[str], via_root: bool) -> sql.Composed:
    return sql.SQL("publish = {}, publish_via_partition_root = {}").format(
        literal(", ".join(operations)), literal(via_root)
    )


async def create_publication(
    conn: AsyncConnection, name: str, *, operations: Sequence[str], via_root: bool, comment: str
) -> None:
    """Create an empty publication; tables are added one at a time afterwards."""
    await execute(
        conn,
        sql.SQL("CREATE PUBLICATION {} WITH ({})").format(ident(name), _options(operations, via_root)),
    )
    await execute(
        conn, sql.SQL("COMMENT ON PUBLICATION {} IS {}").format(ident(name), literal(comment))
    )
    log.info("created publication %s", name)


async def set_publication_options(
    conn: AsyncConnection, name: str, *, operations: Sequence[str], via_root: bool
) -> None:
    await execute(
        conn,
        sql.SQL("ALTER PUBLICATION {} SET ({})").format(ident(name), _options(operations, via_root)),
    )


def _table_spec(
    schema: str, table: str, columns: Sequence[str], row_filter: sql.Composable | None
) -> sql.Composed:
    parts: list[sql.Composable] = [qualified(schema, table)]
    if columns:
        parts.append(sql.SQL("({})").format(idents(list(columns))))
    if row_filter is not None:
        parts.append(sql.SQL("WHERE {}").format(row_filter))
    return sql.SQL(" ").join(parts)


async def add_table(
    conn: AsyncConnection,
    publication: str,
    schema: str,
    table: str,
    *,
    columns: Sequence[str] = (),
    row_filter: sql.Composable | None = None,
) -> None:
    await execute(
        conn,
        sql.SQL("ALTER PUBLICATION {} ADD TABLE {}").format(
            ident(publication), _table_spec(schema, table, columns, row_filter)
        ),
    )


async def drop_table(conn: AsyncConnection, publication: str, schema: str, table: str) -> None:
    await execute(
        conn,
        sql.SQL("ALTER PUBLICATION {} DROP TABLE {}").format(
            ident(publication), qualified(schema, table)
        ),
    )


async def replace_table(
    conn: AsyncConnection,
    publication: str,
    schema: str,
    table: str,
    *,
    columns: Sequence[str] = (),
    row_filter: sql.Composable | None = None,
) -> None:
    """Change a member's columns or filter: drop and re-add it in one transaction.

    Atomic, so a filter the server rejects leaves the old one in place rather
    than the table out of the publication.
    """
    async with conn.transaction():
        await drop_table(conn, publication, schema, table)
        await add_table(conn, publication, schema, table, columns=columns, row_filter=row_filter)


async def drop_publication(conn: AsyncConnection, name: str) -> None:
    await execute(conn, sql.SQL("DROP PUBLICATION IF EXISTS {}").format(ident(name)))
    log.info("dropped publication %s", name)


_PUBLISHED_RELATIONS = sql.SQL(
    "SELECT schemaname || '.' || tablename FROM pg_publication_tables WHERE pubname = %s"
)


async def published_relations(conn: AsyncConnection, publication: str) -> set[str]:
    """What a subscriber of the publication receives: partitions expanded.

    Unlike the publication's own membership, this is in the subscriber's terms -
    each leaf, or the root under publish_via_partition_root - so it compares
    directly with pg_subscription_rel.
    """
    return {str(row[0]) for row in await fetch_all(conn, _PUBLISHED_RELATIONS, (publication,))}


async def nonempty_tables(conn: AsyncConnection, tables: Iterable[tuple[str, str]]) -> set[str]:
    """Which of the named target tables already hold rows."""
    found: set[str] = set()
    for schema, table in sorted(set(tables)):
        if await fetch_scalar(
            conn, sql.SQL("SELECT EXISTS (SELECT 1 FROM {})").format(qualified(schema, table))
        ):
            found.add(f"{schema}.{table}")
    return found
