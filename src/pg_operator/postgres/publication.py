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
class Column:
    """One column of a relation, as :data:`_COLUMNS` reads it."""

    name: str
    type: str
    generated: bool
    not_null: bool
    #: The server can fill it in without the publisher: a default, an identity
    #: or a generated expression.
    supplied: bool


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
    #: The columns the apply worker matches rows by - the replica-identity
    #: index's key columns, or the primary key's. None when the target has
    #: neither, which stops every UPDATE and DELETE the subscription carries.
    identity_columns: frozenset[str] | None = None
    #: NOT NULL columns the server cannot fill in by itself. One of these that
    #: the publication does not carry fails every INSERT the subscription applies.
    required_columns: frozenset[str] = frozenset()

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

#: ``supplied`` is true when the server can fill the column in by itself - it
#: has a default, or is an identity or generated column - which is what decides
#: whether a NOT NULL column the publication does not carry stops apply.
_COLUMNS = sql.SQL(
    """
    SELECT a.attrelid, a.attname, format_type(a.atttypid, a.atttypmod),
           a.attgenerated <> '', a.attnotnull,
           a.atthasdef OR a.attidentity <> '' OR a.attgenerated <> ''
      FROM pg_attribute a
     WHERE a.attrelid = ANY(%s::oid[]) AND a.attnum > 0 AND NOT a.attisdropped
     ORDER BY a.attrelid, a.attnum
    """
)

#: An index's *key* columns only. ``indkey`` also carries the non-key columns of
#: a covering index (``PRIMARY KEY (id) INCLUDE (region)``), which PostgreSQL
#: does not count towards the replica identity - it reads the first
#: ``indnkeyatts`` entries. Taking the whole vector would let a row filter on an
#: INCLUDE column past the gate, and the source would then refuse every UPDATE
#: and DELETE on the table. ``indkey`` is an int2vector, so the slice is
#: zero-based.
_INDEX_KEY_COLUMNS = """
      FROM pg_index i
      CROSS JOIN LATERAL unnest((i.indkey::int2[])[0:i.indnkeyatts - 1]) AS k(attnum)
      JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum
"""

#: The index a replica identity uses: the primary key by default, or the index
#: chosen with REPLICA IDENTITY USING INDEX.
#:
#: A DEFERRABLE primary key is no replica identity at all. PostgreSQL's
#: relcache passes over it when it picks the identity index (``indimmediate``
#: is false), so the source refuses UPDATE and DELETE on a published table whose
#: only key is deferrable. USING INDEX cannot name a deferrable index in the
#: first place, so only the primary-key branch needs the test.
_IDENTITY_COLUMNS = sql.SQL(
    """
    SELECT i.indrelid, a.attname
    """
    + _INDEX_KEY_COLUMNS
    + """
      JOIN pg_class c ON c.oid = i.indrelid
     WHERE i.indrelid = ANY(%s::oid[]) AND i.indisvalid
       AND ((c.relreplident = 'd' AND i.indisprimary AND i.indimmediate)
         OR (c.relreplident = 'i' AND i.indisreplident))
    """
)

#: What a subscriber matches rows by. Unlike the publisher, it falls back to the
#: primary key whenever no replica-identity index applies, so REPLICA IDENTITY
#: NOTHING or FULL on a target that has a primary key still identifies rows -
#: while REPLICA IDENTITY FULL *without* one identifies nothing, because the
#: fallback is to an index, never to the whole row. The fallback skips a
#: DEFERRABLE primary key, exactly as the publisher does.
_TARGET_IDENTITY_COLUMNS = sql.SQL(
    """
    SELECT i.indrelid, i.indisprimary AND i.indimmediate, i.indisreplident, a.attname
    """
    + _INDEX_KEY_COLUMNS
    + """
     WHERE i.indrelid = ANY(%s::oid[]) AND i.indisvalid
       AND ((i.indisprimary AND i.indimmediate) OR i.indisreplident)
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
    columns = _columns_by_relation(await fetch_all(conn, _COLUMNS, (list(by_oid),)))
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
    columns: list[Column],
    identity: set[str] | None,
    leaves: tuple[SourceTable, ...],
) -> SourceTable:
    publishable = {column.name: column.type for column in columns if not column.generated}
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
        generated=frozenset(column.name for column in columns if column.generated),
        readable_by_login=bool(row[10]),
        leaves=leaves,
    )


# ---------------------------------------------------------------------------
# target catalog
# ---------------------------------------------------------------------------

_TARGET_RELATIONS = sql.SQL(
    """
    SELECT c.oid, n.nspname, c.relname, c.relkind, pg_get_userbyid(c.relowner),
           pg_has_role(c.relowner, 'SET'), c.relreplident
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
    columns = _columns_by_relation(await fetch_all(conn, _COLUMNS, (oids,)))
    identity = await _target_identity_columns(conn, oids)
    found: dict[str, TargetTable] = {}
    for row in rows:
        oid = int(row[0])
        of_relation = columns.get(oid, [])
        replicated = [column for column in of_relation if not column.generated]
        generated = [column for column in of_relation if column.generated]
        table = TargetTable(
            schema=str(row[1]),
            name=str(row[2]),
            kind=str(row[3]),
            owner=str(row[4]),
            owner_settable=bool(row[5]),
            columns={column.name: column.type for column in replicated},
            generated=frozenset(column.name for column in generated),
            identity_columns=_subscriber_identity(str(row[6]), identity.get(oid, {})),
            required_columns=frozenset(
                column.name for column in replicated if column.not_null and not column.supplied
            ),
        )
        found[table.qualified] = table
    return found


async def _target_identity_columns(
    conn: AsyncConnection, oids: Sequence[int]
) -> dict[int, dict[str, frozenset[str]]]:
    """Per relation, the key columns of its replica-identity index and of its primary key."""
    gathered: dict[int, dict[str, set[str]]] = {}
    rows = await fetch_all(conn, _TARGET_IDENTITY_COLUMNS, (list(oids),))
    for relid, is_primary, is_replident, name in rows:
        kinds = gathered.setdefault(int(relid), {})
        # One index can be both, so it belongs in both buckets rather than
        # whichever flag is tested first.
        for kind, applies in (("primary", is_primary), ("replident", is_replident)):
            if applies:
                kinds.setdefault(kind, set()).add(str(name))
    return {
        relid: {kind: frozenset(names) for kind, names in kinds.items()}
        for relid, kinds in gathered.items()
    }


def _subscriber_identity(
    replica_identity: str, indexes: dict[str, frozenset[str]]
) -> frozenset[str] | None:
    """The columns the apply worker matches rows by, from a relation's indexes.

    REPLICA IDENTITY USING INDEX names the index; everything else falls back to
    the primary key, including NOTHING and FULL. FULL is not a fallback of its
    own here: the subscriber searches through an index, so a target set to FULL
    with no primary key identifies no row at all.
    """
    if replica_identity == "i" and indexes.get("replident"):
        return indexes["replident"]
    return indexes.get("primary")


def _columns_by_relation(rows: Sequence[tuple[Any, ...]]) -> dict[int, list[Column]]:
    by_relation: dict[int, list[Column]] = {}
    for relid, name, type_, generated, not_null, supplied in rows:
        by_relation.setdefault(int(relid), []).append(
            Column(str(name), str(type_), bool(generated), bool(not_null), bool(supplied))
        )
    return by_relation


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
