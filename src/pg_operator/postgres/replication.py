"""Logical replication: what a server can take part in.

A PostgresReplication needs things from both servers that the operator cannot
set for itself: ``wal_level = logical`` (a restart, and on RDS a static
parameter-group change), spare replication slots and WAL senders, the right to
create subscriptions, and some way to let a login stream from a slot. They are
read here, published on the PostgresInstance, and later checked before a
replication is attempted - so a missing prerequisite is named up front rather
than surfacing as an opaque ``CREATE SUBSCRIPTION`` failure.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from psycopg import AsyncConnection, sql

from .server import ServerInfo
from .sql import fetch_one, fetch_scalar

#: PostgreSQL 16 is the first release in which a non-superuser can own a
#: subscription (``pg_create_subscription``, ``run_as_owner``).
MIN_REPLICATION_VERSION = 16

#: How the managing role may come to own a subscription. ``Assumed`` is RDS:
#: AWS documents the master user creating subscriptions, but not the mechanism,
#: and ``rds_superuser`` is not a member of ``pg_create_subscription``.
SubscriptionPrivilege = Literal["Granted", "Assumed", "Unavailable"]

#: How the managing role may let a login stream from a slot. ``rds_replication``
#: is RDS's grantable stand-in for the REPLICATION attribute; ``attribute`` means
#: the managing role may confer REPLICATION itself; ``none`` leaves only a login
#: provisioned by someone else.
ReplicationGrant = Literal["rds_replication", "attribute", "none"]


@dataclass(frozen=True, slots=True)
class LogicalReplicationInfo:
    """A server's logical replication settings and the managing role's rights."""

    major_version: int
    current_user: str
    wal_level: str
    max_replication_slots: int
    replication_slots_in_use: int
    max_wal_senders: int
    max_slot_wal_keep_size: str
    create_subscription: SubscriptionPrivilege
    replication_grant: ReplicationGrant
    #: ``rds.logical_replication`` exists, so remedies name the parameter group.
    is_rds: bool

    @property
    def wal_is_logical(self) -> bool:
        return self.wal_level == "logical"

    def issues(self) -> list[str]:
        """What stops this server taking part in a replication, with remedies.

        Informational: nothing here fails the instance, because an instance
        that no replication uses has nothing to fix.
        """
        found: list[str] = []
        if self.major_version < MIN_REPLICATION_VERSION:
            found.append(
                f"PostgreSQL {self.major_version} is below the "
                f"{MIN_REPLICATION_VERSION} that replication requires"
            )
        if not self.wal_is_logical:
            if self.is_rds:
                found.append(
                    f"wal_level is {self.wal_level}; set rds.logical_replication "
                    "= 1 in the DB parameter group and reboot (static parameter)"
                )
            else:
                found.append(
                    f"wal_level is {self.wal_level}; set wal_level = logical "
                    "and restart the server"
                )
        if self.max_wal_senders == 0:
            found.append("max_wal_senders is 0, so no subscriber can connect")
        if self.wal_is_logical and self.replication_slots_in_use >= self.max_replication_slots:
            found.append(
                f"all {self.max_replication_slots} replication slots are in use"
            )
        if self.create_subscription == "Unavailable":
            found.append(
                f"{self.current_user} cannot create subscriptions (needs "
                "pg_create_subscription or superuser), so this server cannot "
                "be a replication target"
            )
        if self.replication_grant == "none":
            found.append(
                f"{self.current_user} can neither grant rds_replication nor "
                "confer REPLICATION, so replications from this server need a "
                "pre-provisioned login (source.credentialsSecretRef)"
            )
        return found

    def as_status(self) -> dict[str, Any]:
        return {
            "walLevel": self.wal_level,
            "maxReplicationSlots": self.max_replication_slots,
            "replicationSlotsInUse": self.replication_slots_in_use,
            "maxWalSenders": self.max_wal_senders,
            "maxSlotWalKeepSize": self.max_slot_wal_keep_size,
            "createSubscription": self.create_subscription,
            "replicationGrant": self.replication_grant,
            "issues": self.issues(),
        }


_SETTINGS_QUERY = sql.SQL(
    """
    SELECT current_setting('wal_level'),
           current_setting('max_replication_slots')::int,
           current_setting('max_wal_senders')::int,
           current_setting('max_slot_wal_keep_size'),
           current_setting('rds.logical_replication', true) IS NOT NULL,
           (SELECT count(*) FROM pg_replication_slots)::int,
           (SELECT rolreplication FROM pg_roles WHERE rolname = current_user)
    """
)


async def describe_logical_replication(
    conn: AsyncConnection, server: ServerInfo
) -> LogicalReplicationInfo:
    """Read the settings and rights a replication on this server would need."""
    row = await fetch_one(conn, _SETTINGS_QUERY)
    if row is None:  # pragma: no cover - a bare SELECT always returns a row
        raise RuntimeError("logical replication settings query returned nothing")
    has_replication_attribute = bool(row[6])

    return LogicalReplicationInfo(
        major_version=server.major_version,
        current_user=server.current_user,
        wal_level=str(row[0]),
        max_replication_slots=int(row[1]),
        max_wal_senders=int(row[2]),
        max_slot_wal_keep_size=str(row[3]),
        is_rds=bool(row[4]),
        replication_slots_in_use=int(row[5]),
        create_subscription=await _subscription_privilege(conn, server),
        replication_grant=await _replication_grant(
            conn, server, has_replication_attribute=has_replication_attribute
        ),
    )


async def _subscription_privilege(
    conn: AsyncConnection, server: ServerInfo
) -> SubscriptionPrivilege:
    if server.is_superuser:
        return "Granted"
    # pg_create_subscription only exists from 16; naming it earlier is an error.
    if server.major_version >= MIN_REPLICATION_VERSION and await fetch_scalar(
        conn,
        sql.SQL("SELECT pg_has_role(current_user, 'pg_create_subscription', 'USAGE')"),
    ):
        return "Granted"
    if server.is_rds_superuser:
        return "Assumed"
    return "Unavailable"


async def _replication_grant(
    conn: AsyncConnection, server: ServerInfo, *, has_replication_attribute: bool
) -> ReplicationGrant:
    # rds_replication exists only on RDS (and in this repository's harness,
    # which emulates it); probing for a missing role raises undefined_object.
    if await fetch_scalar(
        conn,
        sql.SQL("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rds_replication')"),
    ) and await fetch_scalar(
        conn,
        sql.SQL(
            "SELECT pg_has_role(current_user, 'rds_replication', "
            "'MEMBER WITH ADMIN OPTION')"
        ),
    ):
        return "rds_replication"
    # Creating a REPLICATION role takes REPLICATION yourself, or superuser.
    if server.is_superuser or (has_replication_attribute and server.can_create_role):
        return "attribute"
    return "none"
