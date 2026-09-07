"""Role primitives: NOLOGIN group roles, LOGIN users, membership and teardown.

Roles in PostgreSQL are cluster-wide, so everything here runs against the
instance's maintenance database.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import logging
from dataclasses import dataclass

from psycopg import AsyncConnection, sql

from ..errors import ObjectMissing, ReconcileFailure
from .settings import parse_setconfig
from .sql import (
    execute,
    fetch_column,
    fetch_one,
    fetch_scalar,
    ident,
    idents,
    literal,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RoleState:
    """Observed attributes of an existing role."""

    name: str
    can_login: bool
    inherit: bool
    create_db: bool
    create_role: bool
    replication: bool
    bypass_rls: bool
    connection_limit: int
    valid_until: str | None


_ROLE_QUERY = sql.SQL(
    """
    SELECT rolname, rolcanlogin, rolinherit, rolcreatedb, rolcreaterole,
           rolreplication, rolbypassrls, rolconnlimit,
           to_char(rolvaliduntil, 'YYYY-MM-DD"T"HH24:MI:SSOF')
      FROM pg_roles
     WHERE rolname = %s
    """
)


async def get_role(conn: AsyncConnection, name: str) -> RoleState | None:
    row = await fetch_one(conn, _ROLE_QUERY, (name,))
    if row is None:
        return None
    return RoleState(
        name=str(row[0]),
        can_login=bool(row[1]),
        inherit=bool(row[2]),
        create_db=bool(row[3]),
        create_role=bool(row[4]),
        replication=bool(row[5]),
        bypass_rls=bool(row[6]),
        connection_limit=int(row[7]),
        valid_until=row[8],
    )


async def role_exists(conn: AsyncConnection, name: str) -> bool:
    return await get_role(conn, name) is not None


async def ensure_group_role(conn: AsyncConnection, name: str) -> bool:
    """Create a NOLOGIN, INHERIT group role if absent. Returns True if created.

    INHERIT matters: members must pick up the group's privileges automatically,
    without an explicit ``SET ROLE``.
    """
    existing = await get_role(conn, name)
    if existing is not None:
        # Only correct what drifted; restating an attribute can be refused
        # outright when the managing role does not hold it (see
        # _attribute_clauses).
        clauses = _attribute_clauses(
            {"login": False, "inherit": True}, observed_attributes(existing)
        )
        if clauses:
            await execute(
                conn,
                sql.SQL("ALTER ROLE {} WITH {}").format(
                    ident(name), sql.SQL(" ").join(clauses)
                ),
            )
            log.info("corrected attributes on group role %s", name)
        return False
    await execute(
        conn, sql.SQL("CREATE ROLE {} WITH NOLOGIN INHERIT").format(ident(name))
    )
    log.info("created group role %s", name)
    return True


#: PostgreSQL's attribute defaults for a newly created role, used as the
#: baseline on the create path so only non-default clauses are emitted.
ROLE_DEFAULTS: dict[str, bool] = {
    "login": False,
    "inherit": True,
    "createDB": False,
    "createRole": False,
    "replication": False,
    "bypassRLS": False,
}

#: (positive, negative) keyword for each attribute.
ATTRIBUTE_KEYWORDS: dict[str, tuple[str, str]] = {
    "login": ("LOGIN", "NOLOGIN"),
    "inherit": ("INHERIT", "NOINHERIT"),
    "createDB": ("CREATEDB", "NOCREATEDB"),
    "createRole": ("CREATEROLE", "NOCREATEROLE"),
    "replication": ("REPLICATION", "NOREPLICATION"),
    "bypassRLS": ("BYPASSRLS", "NOBYPASSRLS"),
}


def observed_attributes(state: RoleState) -> dict[str, bool]:
    return {
        "login": state.can_login,
        "inherit": state.inherit,
        "createDB": state.create_db,
        "createRole": state.create_role,
        "replication": state.replication,
        "bypassRLS": state.bypass_rls,
    }


def _attribute_clauses(
    desired: dict[str, bool], observed: dict[str, bool]
) -> list[sql.Composable]:
    """Keywords for the attributes that actually differ.

    Emitting only differences is not just tidiness. PostgreSQL refuses
    ``ALTER ROLE ... NOREPLICATION`` from a role that lacks REPLICATION *even
    when the target already has NOREPLICATION*, and the RDS master user holds
    neither REPLICATION nor BYPASSRLS — so restating the current value would
    make every reconcile of an existing user fail.
    """
    clauses: list[sql.Composable] = []
    for attribute, (positive, negative) in ATTRIBUTE_KEYWORDS.items():
        if attribute not in desired:
            continue
        if desired[attribute] == observed.get(attribute):
            continue
        clauses.append(sql.SQL(positive if desired[attribute] else negative))
    return clauses


async def valid_until_matches(
    conn: AsyncConnection, name: str, value: str | None
) -> bool:
    """Compare a role's expiry to ``value`` in the server's own type system.

    Comparing formatted timestamps in Python would be defeated by timezone and
    precision differences, so let PostgreSQL do it.
    """
    if value is None:
        unset = await fetch_scalar(
            conn,
            sql.SQL(
                "SELECT rolvaliduntil IS NULL OR rolvaliduntil = 'infinity' "
                "FROM pg_roles WHERE rolname = %s"
            ),
            (name,),
        )
        return bool(unset)
    matches = await fetch_scalar(
        conn,
        sql.SQL("SELECT rolvaliduntil = %s::timestamptz FROM pg_roles WHERE rolname = %s"),
        (value, name),
    )
    return bool(matches)


async def ensure_login_role(
    conn: AsyncConnection,
    name: str,
    *,
    password: str | None,
    attributes: dict[str, bool],
    connection_limit: int = -1,
    valid_until: str | None = None,
) -> bool:
    """Create or converge a login role. Returns True if the role was created.

    On the converge path only differing attributes are altered, so a
    steady-state reconcile issues no statement at all. The password is sent
    only on creation, or via :func:`set_password`, keeping a steady-state pass
    free of credential writes.
    """
    existing = await get_role(conn, name)

    if existing is None:
        clauses = _attribute_clauses(attributes, ROLE_DEFAULTS)
        if connection_limit != -1:
            clauses.append(
                sql.SQL("CONNECTION LIMIT {}").format(literal(connection_limit))
            )
        if valid_until:
            clauses.append(sql.SQL("VALID UNTIL {}").format(literal(valid_until)))
        if password is not None:
            clauses.append(sql.SQL("PASSWORD {}").format(literal(password)))
        statement = sql.SQL("CREATE ROLE {}").format(ident(name))
        if clauses:
            statement = sql.SQL("{} WITH {}").format(
                statement, sql.SQL(" ").join(clauses)
            )
        await execute(conn, statement, describe=f"CREATE ROLE {name} WITH <options>")
        log.info("created login role %s", name)
        return True

    clauses = _attribute_clauses(attributes, observed_attributes(existing))
    if existing.connection_limit != connection_limit:
        clauses.append(
            sql.SQL("CONNECTION LIMIT {}").format(literal(connection_limit))
        )
    if not await valid_until_matches(conn, name, valid_until):
        clauses.append(
            sql.SQL("VALID UNTIL {}").format(literal(valid_until or "infinity"))
        )

    if clauses:
        await execute(
            conn,
            sql.SQL("ALTER ROLE {} WITH {}").format(
                ident(name), sql.SQL(" ").join(clauses)
            ),
            describe=f"ALTER ROLE {name} WITH <options>",
        )
    return False


async def set_password(conn: AsyncConnection, name: str, password: str) -> None:
    """Set a role's password. Never logged, never bound as a parameter."""
    await execute(
        conn,
        sql.SQL("ALTER ROLE {} WITH PASSWORD {}").format(ident(name), literal(password)),
        describe=f"ALTER ROLE {name} WITH PASSWORD <redacted>",
    )


async def password_matches(
    conn: AsyncConnection, name: str, password: str
) -> bool | None:
    """Check a stored password against a candidate without connecting as the role.

    Returns ``None`` when the check cannot be performed — the managing role
    cannot read ``pg_authid`` (the usual case on RDS), or the stored verifier is
    a format this cannot verify. Callers treat ``None`` as "assume it matches"
    so a reconcile never rewrites a working credential on a guess.
    """
    verifier = await _stored_verifier(conn, name)
    if verifier is None:
        return None
    if verifier.startswith("SCRAM-SHA-256$"):
        return await _scram_matches(conn, verifier, password)
    if verifier.startswith("md5"):
        return await _md5_matches(conn, name, verifier, password)
    return None


async def _stored_verifier(conn: AsyncConnection, name: str) -> str | None:
    try:
        return await fetch_scalar(
            conn, sql.SQL("SELECT rolpassword FROM pg_authid WHERE rolname = %s"), (name,)
        )
    except Exception:  # noqa: BLE001 - pg_authid is superuser-only on RDS
        return None


async def _scram_matches(conn: AsyncConnection, verifier: str, password: str) -> bool | None:
    """Verify a SCRAM-SHA-256 verifier by re-deriving StoredKey from the password.

    Format: ``SCRAM-SHA-256$<iterations>:<b64 salt>$<b64 StoredKey>:<b64 ServerKey>``
    where ``StoredKey = SHA256(HMAC(PBKDF2(password, salt, iterations), "Client Key"))``.
    """
    del conn
    parts = verifier.split("$")
    if len(parts) != 3:
        return None
    try:
        iterations_raw, salt_b64 = parts[1].split(":", 1)
        stored_key_b64 = parts[2].split(":", 1)[0]
        salt = base64.b64decode(salt_b64, validate=True)
        iterations = int(iterations_raw)
    except (ValueError, binascii.Error):
        return None

    salted = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    derived = base64.b64encode(hashlib.sha256(client_key).digest()).decode()
    return hmac.compare_digest(derived, stored_key_b64)


async def _md5_matches(
    conn: AsyncConnection, name: str, verifier: str, password: str
) -> bool | None:
    del conn
    digest = hashlib.md5((password + name).encode(), usedforsecurity=False).hexdigest()
    return hmac.compare_digest(f"md5{digest}", verifier)


async def memberships(conn: AsyncConnection, member: str) -> set[str]:
    """Direct role memberships held by ``member``."""
    rows = await fetch_column(
        conn,
        sql.SQL(
            """
            SELECT g.rolname
              FROM pg_auth_members m
              JOIN pg_roles g ON g.oid = m.roleid
              JOIN pg_roles u ON u.oid = m.member
             WHERE u.rolname = %s
            """
        ),
        (member,),
    )
    return set(rows)


async def members_of(conn: AsyncConnection, group: str) -> set[str]:
    """Direct members of ``group``."""
    rows = await fetch_column(
        conn,
        sql.SQL(
            """
            SELECT u.rolname
              FROM pg_auth_members m
              JOIN pg_roles g ON g.oid = m.roleid
              JOIN pg_roles u ON u.oid = m.member
             WHERE g.rolname = %s
            """
        ),
        (group,),
    )
    return set(rows)


async def grant_membership(
    conn: AsyncConnection, group: str, member: str, *, with_admin: bool = False
) -> None:
    statement = sql.SQL("GRANT {} TO {}").format(ident(group), ident(member))
    if with_admin:
        statement = sql.SQL("{} WITH ADMIN OPTION").format(statement)
    await execute(conn, statement)


async def revoke_membership(conn: AsyncConnection, group: str, member: str) -> None:
    await execute(
        conn, sql.SQL("REVOKE {} FROM {}").format(ident(group), ident(member))
    )


async def ensure_self_membership(
    conn: AsyncConnection, group: str, *, per_grant_options: bool
) -> None:
    """Give the managing role the rights it needs to act as ``group``.

    Two distinct rights are needed, and on PostgreSQL 16+ neither is granted by
    default even to the role that created the group:

    * ``SET`` — required to name the group as an owner
      (``CREATE DATABASE ... OWNER``, ``ALTER DATABASE ... OWNER TO``).
    * ``INHERIT`` — required to then *act* as the owner: ``CREATE SCHEMA``,
      ``ALTER DATABASE ... SET``, ``ALTER DEFAULT PRIVILEGES``,
      ``CREATE EXTENSION``.

    From PostgreSQL 16 a CREATEROLE role that creates a role receives
    ``ADMIN OPTION`` but ``INHERIT FALSE, SET FALSE``, so membership alone is
    not enough — and because ``GRANT`` and ``REVOKE`` only *warn* when the
    grantor lacks authority, getting this wrong produces a database with no
    grants on it rather than an error. ``per_grant_options`` selects the
    PostgreSQL 16+ syntax; earlier versions control inheritance through the
    member role's own ``rolinherit``.
    """
    current = str(await fetch_scalar(conn, sql.SQL("SELECT current_user")))
    if current == group:
        return

    if per_grant_options:
        has_set = await fetch_scalar(
            conn, sql.SQL("SELECT pg_has_role(current_user, %s, 'SET')"), (group,)
        )
        has_inherit = await fetch_scalar(
            conn, sql.SQL("SELECT pg_has_role(current_user, %s, 'USAGE')"), (group,)
        )
        if has_set and has_inherit:
            return
        # Re-granting updates the options on an existing membership. ADMIN
        # OPTION is deliberately not requested: PostgreSQL refuses to grant it
        # back to the grantor, and the automatic grant already carries it.
        await execute(
            conn,
            sql.SQL("GRANT {} TO {} WITH INHERIT TRUE, SET TRUE").format(
                ident(group), ident(current)
            ),
        )
        log.info("granted %s to managing role %s with INHERIT and SET", group, current)
        return

    is_member = await fetch_scalar(
        conn, sql.SQL("SELECT pg_has_role(current_user, %s, 'MEMBER')"), (group,)
    )
    if not is_member:
        await grant_membership(conn, group, current, with_admin=True)
        log.info("granted %s to managing role %s", group, current)

    can_use = await fetch_scalar(
        conn, sql.SQL("SELECT pg_has_role(current_user, %s, 'USAGE')"), (group,)
    )
    if not can_use:
        # rolinherit is an attribute of the member role before PostgreSQL 16,
        # so no grant can fix this; say so rather than silently under-granting.
        log.warning(
            "managing role %s is a member of %s but does not inherit its "
            "privileges (NOINHERIT); grants on the database may not apply",
            current,
            group,
        )


async def set_role_parameters(
    conn: AsyncConnection, name: str, parameters: dict[str, str], observed: dict[str, str]
) -> list[str]:
    """Converge ``ALTER ROLE ... SET`` settings; returns the changes applied."""
    changes: list[str] = []
    for key, value in parameters.items():
        if observed.get(key) != value:
            await execute(
                conn,
                sql.SQL("ALTER ROLE {} SET {} TO {}").format(
                    ident(name), ident(key), literal(value)
                ),
            )
            changes.append(f"set {key}")
    for key in observed.keys() - parameters.keys():
        await execute(
            conn, sql.SQL("ALTER ROLE {} RESET {}").format(ident(name), ident(key))
        )
        changes.append(f"reset {key}")
    return changes


async def get_role_parameters(
    conn: AsyncConnection, name: str, database: str | None = None
) -> dict[str, str]:
    """Read ``pg_db_role_setting`` entries for a role, optionally scoped to a database."""
    rows = await fetch_column(
        conn,
        sql.SQL(
            """
            SELECT s.setconfig
              FROM pg_db_role_setting s
              JOIN pg_roles r ON r.oid = s.setrole
              LEFT JOIN pg_database d ON d.oid = s.setdatabase
             WHERE r.rolname = %s
               AND coalesce(d.datname, '') = %s
            """
        ),
        (name, database or ""),
    )
    settings: dict[str, str] = {}
    for config in rows:
        settings.update(parse_setconfig(config))
    return settings


async def set_default_role_in_database(
    conn: AsyncConnection, login_role: str, database: str, group_role: str
) -> None:
    """Make ``login_role`` assume ``group_role`` when connected to ``database``.

    Without this, a human or migration job in the owner group creates tables
    owned by their own login role, and the group's DEFAULT PRIVILEGES never
    apply — the classic cause of "the RW user cannot see the new table".
    """
    await execute(
        conn,
        sql.SQL("ALTER ROLE {} IN DATABASE {} SET role TO {}").format(
            ident(login_role), ident(database), ident(group_role)
        ),
    )


async def reset_default_role_in_database(
    conn: AsyncConnection, login_role: str, database: str
) -> bool:
    """Clear a per-database ``role`` setting. False if the database is gone.

    Dropping a database takes its per-role settings with it, so a missing
    database means the work is already done. Callers race with exactly that:
    a PostgresDB and the PostgresUsers referencing it are normally deleted
    together.
    """
    try:
        await execute(
            conn,
            sql.SQL("ALTER ROLE {} IN DATABASE {} RESET role").format(
                ident(login_role), ident(database)
            ),
        )
    except ObjectMissing:
        log.info(
            "database %s no longer exists; nothing to reset for %s",
            database,
            login_role,
        )
        return False
    return True


async def owned_objects_exist(conn: AsyncConnection, role: str) -> bool:
    """Whether the role still owns databases, which would block DROP ROLE."""
    count = await fetch_scalar(
        conn,
        sql.SQL(
            "SELECT count(*) FROM pg_database d "
            "JOIN pg_roles r ON r.oid = d.datdba WHERE r.rolname = %s"
        ),
        (role,),
    )
    return bool(count)


async def drop_owned(conn: AsyncConnection, role: str, *, cascade: bool = False) -> None:
    """``DROP OWNED BY`` inside the current database, clearing grants and objects."""
    statement = sql.SQL("DROP OWNED BY {}").format(ident(role))
    if cascade:
        statement = sql.SQL("{} CASCADE").format(statement)
    await execute(conn, statement)


async def drop_role(conn: AsyncConnection, name: str) -> None:
    await execute(conn, sql.SQL("DROP ROLE IF EXISTS {}").format(ident(name)))
    log.info("dropped role %s", name)


async def terminate_role_sessions(conn: AsyncConnection, name: str) -> int:
    """Terminate every backend authenticated as ``name``, tolerating refusals.

    Call after :func:`ensure_self_membership` for the role: membership in a
    backend's role is what makes signalling it legal for a non-superuser.
    """
    pids = await fetch_column(
        conn,
        sql.SQL(
            "SELECT pid FROM pg_stat_activity "
            "WHERE usename = %s AND pid <> pg_backend_pid()"
        ),
        (name,),
    )
    terminated = 0
    for pid in pids:
        try:
            await fetch_scalar(
                conn, sql.SQL("SELECT pg_terminate_backend(%s)"), (int(pid),)
            )
            terminated += 1
        except ReconcileFailure:
            log.warning("could not terminate session %s for role %s", pid, name)
    return terminated


async def revoke_all_memberships(conn: AsyncConnection, member: str) -> list[str]:
    """Revoke every group membership held by ``member``."""
    held = await memberships(conn, member)
    if held:
        await execute(
            conn,
            sql.SQL("REVOKE {} FROM {}").format(
                idents(sorted(held)), ident(member)
            ),
        )
    return sorted(held)
