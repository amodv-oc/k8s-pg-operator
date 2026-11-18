"""What a PostgresReplication's author, or a target table's owner, must not be able to reach.

The operator works as each server's managing role - on RDS, the master user -
and builds a connection string holding a password that the *target server*
sends wherever ``source.connection`` says. Neither may be turned against the
servers: a tenant's relation must not run code as the managing role, and the
password must not reach a host that merely asks for it.
"""

from __future__ import annotations

import asyncio
import os
import socket
import struct
import threading

import pytest

from pg_operator.errors import ConfigurationError, SourceUnreachableFromTarget

from ..conftest import requires_replication_harness, superuser_execute
from .conftest import NS, OPERATOR_NS, World

pytestmark = [requires_replication_harness, pytest.mark.replication, pytest.mark.asyncio]

#: This machine, as the target *server* resolves it. Docker Desktop and
#: OrbStack provide the name; hack/test-servers.sh adds it on Linux.
HOST_FROM_TARGET = os.environ.get("PGOP_TEST_HOST_FROM_TARGET", "host.docker.internal")

#: Records who ran it. SECURITY INVOKER, as a function is by default.
PROBE = (
    "CREATE TABLE public.seen (who text)",
    "GRANT INSERT ON public.seen TO PUBLIC",
    """CREATE FUNCTION public.probe() RETURNS boolean LANGUAGE plpgsql VOLATILE AS $$
       BEGIN INSERT INTO public.seen VALUES (current_user); RETURN true; END $$""",
)


async def seen(world: World) -> set[str]:
    found = await world.target_value("SELECT array_agg(DISTINCT who) FROM public.seen")
    return set(found or [])


# --- the target's relations are a tenant's; the managing role only looks -------


async def test_a_view_named_like_a_table_is_never_read(world: World) -> None:
    """A view's functions run as whoever selects from it.

    public.only_source exists on the source alone, so its name is free on the
    target - where anyone who may create a view can take it.
    """
    await world.on_target(
        *PROBE,
        "CREATE VIEW public.only_source AS SELECT 1 AS id WHERE public.probe()",
        as_owner=True,
    )
    status = await world.run()

    assert await seen(world) == set()
    # A view is not somewhere a subscription can apply into.
    assert world.excluded()["public.only_source"] == "TargetTableMissing"
    assert "public.only_source" not in status["publishedTables"]


async def test_a_table_is_checked_for_rows_as_its_owner(world: World) -> None:
    """A policy on a FORCE ROW LEVEL SECURITY table runs for its owner too.

    The managing role holds the owner's privileges, so selecting as itself
    would run the policy's function as the managing role.
    """
    owner = world.target_db + "_owner"
    await world.on_target(
        *PROBE,
        "CREATE TABLE public.only_source (id int PRIMARY KEY)",
        "INSERT INTO public.only_source VALUES (1)",
        "ALTER TABLE public.only_source ENABLE ROW LEVEL SECURITY",
        "ALTER TABLE public.only_source FORCE ROW LEVEL SECURITY",
        "CREATE POLICY look ON public.only_source USING (public.probe())",
        as_owner=True,
    )
    await world.run()

    assert await seen(world) == {owner}
    assert world.target.username not in await seen(world)
    # The probe still does its job: the table has rows, so its copy waits.
    assert world.excluded()["public.only_source"] == "TargetTableNotEmpty"


# --- the replication password ----------------------------------------------------


def _password_harvester(captured: dict[str, str]) -> tuple[socket.socket, int]:
    """A server that answers a connection by asking for the password in cleartext."""
    server = socket.socket()
    server.bind(("0.0.0.0", 0))  # noqa: S104 - the target container must reach it
    server.listen(1)

    def read_message(conn: socket.socket) -> bytes:
        (length,) = struct.unpack("!I", conn.recv(4))
        return conn.recv(length - 4)

    def serve() -> None:
        try:
            conn, _ = server.accept()
        except OSError:
            return  # closed before anything connected
        with conn:
            try:
                body = read_message(conn)
                if struct.unpack("!I", body[:4])[0] == 80877103:  # SSLRequest
                    conn.sendall(b"N")
                    body = read_message(conn)
                parameters = body[4:].split(b"\0")
                captured["user"] = parameters[parameters.index(b"user") + 1].decode()
                conn.sendall(b"R" + struct.pack("!II", 8, 3))  # AuthenticationCleartextPassword
                if conn.recv(1) == b"p":
                    captured["password"] = read_message(conn).rstrip(b"\0").decode()
            except (OSError, struct.error, ValueError):
                pass  # the client hung up, which is the point

    threading.Thread(target=serve, daemon=True).start()
    return server, server.getsockname()[1]


async def test_the_password_is_not_given_to_a_host_that_asks_for_it(world: World, unique: str) -> None:
    """source.connection.host is free-form: it can name a server that is not the source.

    libpq hands a cleartext password to any server that requests one unless
    told what to insist on, so the connection string insists on SCRAM - which
    proves the password without disclosing it.
    """
    login, password = f"rs_byo_{unique}", f"byo-{unique}-password"
    superuser_execute(f"CREATE ROLE \"{login}\" LOGIN REPLICATION PASSWORD '{password}'")
    captured: dict[str, str] = {}
    server, port = _password_harvester(captured)
    try:
        world.k8s.core.seed(NS, "byo-login", {"username": login, "password": password})
        world.replication["spec"]["source"]["credentialsSecretRef"] = {"name": "byo-login"}
        world.replication["spec"]["source"]["connection"] = {
            "host": HOST_FROM_TARGET, "port": port, "sslMode": "disable",
        }
        with pytest.raises(SourceUnreachableFromTarget) as refused:
            await world.run()
        for _ in range(20):
            if "password" in captured:
                break
            await asyncio.sleep(0.1)
    finally:
        server.close()
        superuser_execute(f'DROP ROLE IF EXISTS "{login}"')

    if "user" not in captured:
        pytest.skip(f"the target server cannot reach this machine as {HOST_FROM_TARGET}")
    assert captured == {"user": login}
    assert "SCRAM" in str(refused.value)
    assert password not in str(refused.value)


async def test_the_managing_role_is_refused_as_the_replication_login(world: World) -> None:
    """Its password would be stored in the target's catalog and sent to source.connection.host."""
    world.k8s.core.seed(
        NS, "borrowed", {"username": world.source.username, "password": world.source.password}
    )
    world.replication["spec"]["source"]["credentialsSecretRef"] = {"name": "borrowed"}
    with pytest.raises(ConfigurationError, match="managing role"):
        await world.run()


async def test_a_secret_in_another_namespace_cannot_be_named(world: World) -> None:
    """The operator reads Secrets cluster-wide; a tenant's reference must not borrow that reach."""
    world.replication["spec"]["source"]["credentialsSecretRef"] = {
        "name": "src-superuser", "namespace": OPERATOR_NS,
    }
    with pytest.raises(ConfigurationError, match="invalid spec"):
        await world.run()
