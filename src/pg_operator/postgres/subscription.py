"""Subscriptions on the target, and the replication slots they hold on the source.

A subscription's connection string carries the replication login's password,
so every statement that includes it is executed with a redacted rendering:
the password reaches neither the operator's logs nor a status message. (It
does reach the target's own catalog, readable only by superusers, and its
statement log if ``log_statement`` is ``ddl`` or ``all`` - a documented
operating caveat.)
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Sequence
from dataclasses import dataclass

import psycopg
from psycopg import AsyncConnection, sql

from ..errors import ReconcileFailure
from .sql import execute, fetch_all, fetch_one, fetch_scalar, ident, literal

log = logging.getLogger(__name__)

#: pg_subscription.substream, as PostgreSQL 16 stores it.
_STREAMING = {"f": "off", "t": "on", "p": "parallel"}


@dataclass(frozen=True, slots=True)
class SubscriptionState:
    oid: int
    name: str
    owner: str
    comment: str | None
    enabled: bool
    binary: bool
    streaming: str
    slot_name: str | None
    publications: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SlotState:
    name: str
    #: A logical slot belongs to one database; a subscription must stream from it there.
    database: str | None
    active: bool
    active_pid: int | None


def build_conninfo(
    *,
    host: str,
    port: int,
    database: str,
    user: str,
    password: str,
    ssl_mode: str,
    ssl_root_cert: str | None,
) -> str:
    """The connection string the target server uses to reach the source.

    ``connect_timeout`` bounds how long CREATE and DROP SUBSCRIPTION wait on a
    source the target cannot route to, rather than leaving that to the
    statement timeout.
    """
    params = {
        "host": host,
        "port": str(port),
        "dbname": database,
        "user": user,
        "password": password,
        "sslmode": ssl_mode,
        "connect_timeout": "10",
    }
    if ssl_root_cert:
        params["sslrootcert"] = ssl_root_cert
    return psycopg.conninfo.make_conninfo(**params)


def conninfo_fingerprint(conninfo: str) -> str:
    """A hash of the connection string, recorded in status in its place.

    The target's ``subconninfo`` cannot be read back by a non-superuser, so
    this is how a pass knows whether ``ALTER SUBSCRIPTION ... CONNECTION`` is
    needed. The password is high-entropy, so its hash reveals nothing.
    """
    return hashlib.sha256(conninfo.encode()).hexdigest()[:16]


_SUBSCRIPTION = sql.SQL(
    """
    SELECT s.oid, pg_get_userbyid(s.subowner), obj_description(s.oid, 'pg_subscription'),
           s.subenabled, s.subbinary, s.substream::text, s.subslotname, s.subpublications
      FROM pg_subscription s
     WHERE s.subname = %s
       AND s.subdbid = (SELECT oid FROM pg_database WHERE datname = current_database())
    """
)

_SUBSCRIBED_TABLES = sql.SQL(
    """
    SELECT n.nspname || '.' || c.relname, sr.srsubstate
      FROM pg_subscription_rel sr
      JOIN pg_class c ON c.oid = sr.srrelid
      JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE sr.srsubid = %s
    """
)


async def get_subscription(conn: AsyncConnection, name: str) -> SubscriptionState | None:
    """The subscription in the connected database. Never reads ``subconninfo``."""
    row = await fetch_one(conn, _SUBSCRIPTION, (name,))
    if row is None:
        return None
    return SubscriptionState(
        oid=int(row[0]),
        name=name,
        owner=str(row[1]),
        comment=row[2],
        enabled=bool(row[3]),
        binary=bool(row[4]),
        streaming=_STREAMING.get(str(row[5]), str(row[5])),
        slot_name=row[6],
        publications=tuple(row[7] or ()),
    )


async def subscribed_tables(conn: AsyncConnection, oid: int) -> dict[str, str]:
    """``schema.table -> srsubstate``: ``i`` init, ``d`` copying, ``f``/``s`` syncing, ``r`` ready."""
    return {str(table): str(state) for table, state in await fetch_all(conn, _SUBSCRIBED_TABLES, (oid,))}


async def create_subscription(
    conn: AsyncConnection,
    name: str,
    *,
    conninfo: str,
    publication: str,
    copy_data: bool,
    enabled: bool,
    binary: bool | None,
    streaming: str | None,
    comment: str,
) -> None:
    """Create the subscription on a slot that already exists on the source.

    The slot is never created here (``create_slot = false``): CREATE
    SUBSCRIPTION would create it inside its own transaction on the target, and
    creating a slot waits for every transaction already running on the source.
    Two subscriptions created at once in opposite directions between two
    servers - or round a ring of them - then each wait on the other until the
    statement timeout, and retry into the same deadlock. See :func:`create_slot`.

    Owned by the managing role, with ``run_as_owner`` and ``password_required``
    left at their defaults: changes apply as each table's owner, and the
    connection must authenticate with the password it carries.
    """
    options = [
        sql.SQL("create_slot = false"),
        sql.SQL("slot_name = {}").format(literal(name)),
        sql.SQL("copy_data = {}").format(literal(copy_data)),
        sql.SQL("enabled = {}").format(literal(enabled)),
    ]
    if binary is not None:
        options.append(sql.SQL("binary = {}").format(literal(binary)))
    if streaming is not None:
        options.append(sql.SQL("streaming = {}").format(literal(streaming)))
    await execute(
        conn,
        sql.SQL("CREATE SUBSCRIPTION {} CONNECTION {} PUBLICATION {} WITH ({})").format(
            ident(name), literal(conninfo), ident(publication), sql.SQL(", ").join(options)
        ),
        describe=f"CREATE SUBSCRIPTION {name} CONNECTION <redacted> PUBLICATION {publication}",
    )
    await execute(
        conn, sql.SQL("COMMENT ON SUBSCRIPTION {} IS {}").format(ident(name), literal(comment))
    )
    log.info("created subscription %s", name)


async def set_connection(conn: AsyncConnection, name: str, conninfo: str) -> None:
    await execute(
        conn,
        sql.SQL("ALTER SUBSCRIPTION {} CONNECTION {}").format(ident(name), literal(conninfo)),
        describe=f"ALTER SUBSCRIPTION {name} CONNECTION <redacted>",
    )


async def set_enabled(conn: AsyncConnection, name: str, enabled: bool) -> None:
    verb = sql.SQL("ENABLE") if enabled else sql.SQL("DISABLE")
    await execute(conn, sql.SQL("ALTER SUBSCRIPTION {} {}").format(ident(name), verb))


async def set_options(
    conn: AsyncConnection, name: str, *, binary: bool | None, streaming: str | None
) -> None:
    options: list[sql.Composable] = []
    if binary is not None:
        options.append(sql.SQL("binary = {}").format(literal(binary)))
    if streaming is not None:
        options.append(sql.SQL("streaming = {}").format(literal(streaming)))
    if options:
        await execute(
            conn,
            sql.SQL("ALTER SUBSCRIPTION {} SET ({})").format(ident(name), sql.SQL(", ").join(options)),
        )


async def refresh(conn: AsyncConnection, name: str, *, copy_data: bool) -> None:
    """Pick up the publication's table changes. Cannot run in a transaction block."""
    await execute(
        conn,
        sql.SQL("ALTER SUBSCRIPTION {} REFRESH PUBLICATION WITH (copy_data = {})").format(
            ident(name), literal(copy_data)
        ),
    )


async def drop_subscription(conn: AsyncConnection, name: str) -> bool:
    """Drop the subscription. True if its slot went with it.

    A plain DROP SUBSCRIPTION also drops the slot (and any table-sync slots)
    on the source - through the target's connection to it. When that fails,
    because the target cannot reach the source or the slot is still busy, the
    subscription is detached from its slot and dropped anyway; the caller then
    drops the slots from the source side. Either way the subscription goes.
    """
    await set_enabled(conn, name, False)
    try:
        await execute(conn, sql.SQL("DROP SUBSCRIPTION {}").format(ident(name)))
        return True
    except ReconcileFailure as exc:
        log.warning("DROP SUBSCRIPTION %s could not drop its slot (%s); detaching it", name, exc)
    await execute(conn, sql.SQL("ALTER SUBSCRIPTION {} SET (slot_name = NONE)").format(ident(name)))
    await execute(conn, sql.SQL("DROP SUBSCRIPTION {}").format(ident(name)))
    return False


# ---------------------------------------------------------------------------
# slots, on the source
# ---------------------------------------------------------------------------

_SLOT = sql.SQL(
    "SELECT slot_name, database, active, active_pid FROM pg_replication_slots WHERE slot_name = %s"
)
#: Table-sync slots are named ``pg_<subscription oid>_sync_<relid>_<system id>``.
#: A plain DROP SUBSCRIPTION removes them; a detached one leaves them behind.
_SLOT_AND_SYNC_SLOTS = sql.SQL(
    """
    SELECT slot_name, database, active, active_pid
      FROM pg_replication_slots
     WHERE slot_name = %s OR slot_name LIKE 'pg\\_' || %s || '\\_sync\\_%%'
     ORDER BY slot_name
    """
)


async def slots(conn: AsyncConnection, name: str, *, subscription_oid: int | None) -> list[SlotState]:
    """The subscription's slot and, given its oid, its table-sync slots."""
    if subscription_oid is None:
        rows = await fetch_all(conn, _SLOT, (name,))
    else:
        rows = await fetch_all(conn, _SLOT_AND_SYNC_SLOTS, (name, str(subscription_oid)))
    return [
        SlotState(name=str(row[0]), database=row[1], active=bool(row[2]), active_pid=row[3])
        for row in rows
    ]


async def create_slot(conn: AsyncConnection, name: str) -> None:
    """Create a subscription's slot, in the database ``conn`` is connected to.

    A logical slot belongs to one database, so ``conn`` must be connected to
    the source database. Run in a transaction of its own on the source, the
    wait for that server's running transactions involves no other server.
    Needs REPLICATION, or rds_replication on RDS.
    """
    await execute(
        conn,
        sql.SQL("SELECT pg_create_logical_replication_slot({}, 'pgoutput')").format(literal(name)),
    )
    log.info("created replication slot %s", name)


async def drop_slots(conn: AsyncConnection, names: Sequence[str]) -> None:
    """Drop slots that no walsender holds. Needs REPLICATION, or rds_replication on RDS."""
    for name in names:
        await execute(conn, sql.SQL("SELECT pg_drop_replication_slot({})").format(literal(name)))
        log.info("dropped replication slot %s", name)


async def terminate(conn: AsyncConnection, pid: int) -> bool:
    return bool(await fetch_scalar(conn, sql.SQL("SELECT pg_terminate_backend(%s)"), (pid,)))
