"""PostgresReplication's first reconcile step, and the status it all produces.

Step 1 - resolve both ends, refuse the impossible - never connects to a server,
so it is tested here in full against a fake cluster. The later steps need real
servers and live in tests/integration/test_replication_source.py.
"""

from __future__ import annotations

from typing import Any

import pytest

from pg_operator.config import Settings
from pg_operator.constants import PLURAL_DATABASE, PLURAL_INSTANCE, PLURAL_REPLICATION
from pg_operator.errors import ConfigurationError, ReferenceNotFound
from pg_operator.naming import replication_names
from pg_operator.reconcile.context import ReconcileContext
from pg_operator.reconcile.replication import (
    ReplicationResult,
    build_status,
    resolve_replication,
)

from .fakes import FakeK8sClient

pytestmark = pytest.mark.asyncio

GROUP_VERSION = "postgres.ourcommunity.com.au/v1alpha1"


def _instance(name: str, host: str, **spec: Any) -> dict[str, Any]:
    return {
        "apiVersion": GROUP_VERSION,
        "kind": "PostgresInstance",
        "metadata": {"name": name, "generation": 1},
        "spec": {"host": host, "credentialsSecretRef": {"name": f"{name}-superuser"}, **spec},
    }


def _database(namespace: str, name: str, instance: str, **spec: Any) -> dict[str, Any]:
    return {
        "apiVersion": GROUP_VERSION,
        "kind": "PostgresDB",
        "metadata": {"name": name, "namespace": namespace, "generation": 1},
        "spec": {"instanceRef": {"name": instance}, **spec},
    }


def _replication(
    name: str, source: str, target: str, namespace: str = "team-a", **spec: Any
) -> dict[str, Any]:
    return {
        "apiVersion": GROUP_VERSION,
        "kind": "PostgresReplication",
        "metadata": {"name": name, "namespace": namespace, "generation": 2},
        "spec": {
            "source": {"dbRef": {"name": source}},
            "target": {"dbRef": {"name": target}},
            "schemas": [{"name": "public"}],
            **spec,
        },
    }


@pytest.fixture
def k8s() -> FakeK8sClient:
    """Three servers and a database on each, all in team-a."""
    client = FakeK8sClient()
    for name, host in (("prod", "prod.rds"), ("reporting", "reporting.rds"), ("archive", "archive.rds")):
        client.custom.add(_instance(name, host), PLURAL_INSTANCE)
    client.custom.add(_database("team-a", "orders", "prod", schemas=[{"name": "audit"}]), PLURAL_DATABASE)
    client.custom.add(_database("team-a", "orders-copy", "reporting"), PLURAL_DATABASE)
    client.custom.add(_database("team-a", "orders-archive", "archive"), PLURAL_DATABASE)
    return client


@pytest.fixture
def ctx(k8s: FakeK8sClient) -> ReconcileContext:
    return ReconcileContext(k8s, settings=Settings(operator_namespace="pg-operator"))  # type: ignore[arg-type]


def _store(k8s: FakeK8sClient, obj: dict[str, Any]) -> dict[str, Any]:
    k8s.custom.add(obj, PLURAL_REPLICATION)
    return obj


async def test_both_ends_resolve(ctx, k8s) -> None:
    obj = _store(k8s, _replication("to-reporting", "orders", "orders-copy", schemas=[{"name": "audit"}]))
    resolved = await resolve_replication(ctx, obj)
    assert resolved.source.display == "prod/orders"
    assert resolved.target.display == "reporting/orders_copy"
    assert resolved.source.as_status()["postgresDB"] == "team-a/orders"
    assert resolved.source_instance.host == "prod.rds"


async def _result(ctx, k8s, **fields: Any) -> ReplicationResult:
    resolved = await resolve_replication(ctx, _store(k8s, _replication("to-reporting", "orders", "orders-copy")))
    return ReplicationResult(
        source=resolved.source,
        target=resolved.target,
        names=replication_names("team-a", "to-reporting"),
        login="pgop_repl_x",
        **fields,
    )


async def test_a_subscribed_replication_is_ready(ctx, k8s) -> None:
    result = await _result(
        ctx, k8s, published=["public.a", "public.b"], subscribed={"public.a": "r", "public.b": "d"}
    )
    status = build_status(result, None, 2)
    assert status["phase"] == "Ready"
    conditions = {c["type"]: c for c in status["conditions"]}
    assert conditions["Ready"]["status"] == "True"
    assert conditions["Drifted"]["status"] == "False"
    assert conditions["Synced"]["status"] == "True"
    assert status["tables"] == {
        "published": 2, "subscribed": 2, "ready": 1, "syncing": 1, "excluded": 0, "display": "1/2",
    }
    assert status["source"]["slot"] == status["target"]["subscription"]
    assert "1 of 2 subscribed table(s) ready" in conditions["Ready"]["message"]


async def test_held_back_tables_name_their_cause(ctx, k8s) -> None:
    one = {"table": "public.c", "reason": "TargetTableMissing", "message": "public.c does not exist"}
    result = await _result(ctx, k8s, published=["public.a"], subscribed={"public.a": "r"}, excluded=[one])
    status = build_status(result, None, 2)
    conditions = {c["type"]: c for c in status["conditions"]}
    assert (conditions["Drifted"]["status"], conditions["Drifted"]["reason"]) == ("True", "TargetTableMissing")
    assert conditions["Synced"]["status"] == "False"
    assert "public.c: public.c does not exist" in conditions["Drifted"]["message"]
    assert status["phase"] == "Drifted"
    assert status["tables"]["excluded"] == 1

    two = {"table": "public.d", "reason": "NoReplicaIdentity", "message": "no identity"}
    status = build_status(await _result(ctx, k8s, excluded=[one, two]), None, 2)
    assert {c["type"]: c for c in status["conditions"]}["Drifted"]["reason"] == "MultipleIssues"


async def test_ready_replaces_a_failure_from_an_earlier_pass(ctx, k8s) -> None:
    """Once the cause is fixed, a Ready=False left by the failed pass must go."""
    earlier = {"conditions": [{"type": "Ready", "status": "False", "reason": "Configuration", "message": "x"}]}
    assert build_status(await _result(ctx, k8s), earlier, 2)["phase"] == "Ready"


async def test_the_password_never_reaches_status() -> None:
    from pg_operator.reconcile.replication import ReplicationEnd

    end = ReplicationEnd("team-a", "orders", "prod", "orders", "prod.rds", 5432)
    result = ReplicationResult(
        source=end, target=end, names=replication_names("team-a", "x"), password="s3cret-value"
    )
    assert "s3cret-value" not in repr(result)
    assert "s3cret-value" not in repr(build_status(result, None, 1))


async def test_a_missing_target_is_temporary_and_named(ctx, k8s) -> None:
    obj = _store(k8s, _replication("to-nowhere", "orders", "not-yet"))
    with pytest.raises(ReferenceNotFound, match=r"spec\.target"):
        await resolve_replication(ctx, obj)


async def test_two_instances_on_one_server_are_one_server(ctx, k8s) -> None:
    """A second PostgresInstance pointing at the same host is not a second server."""
    k8s.custom.add(_instance("prod-alias", "PROD.rds"), PLURAL_INSTANCE)
    k8s.custom.add(_database("team-a", "orders-elsewhere", "prod-alias"), PLURAL_DATABASE)
    obj = _store(k8s, _replication("loopback", "orders", "orders-elsewhere"))
    with pytest.raises(ConfigurationError, match="same server"):
        await resolve_replication(ctx, obj)


async def test_the_namespace_must_be_admitted_at_both_ends(ctx, k8s) -> None:
    k8s.custom.add(_instance("locked", "locked.rds", allowedNamespaces=["team-b"]), PLURAL_INSTANCE)
    k8s.custom.add(_database("team-b", "vault", "locked"), PLURAL_DATABASE)
    obj = _store(
        k8s,
        {
            **_replication("into-locked", "orders", "vault"),
            "spec": {
                "source": {"dbRef": {"name": "orders"}},
                "target": {"dbRef": {"name": "vault", "namespace": "team-b"}},
                "schemas": [{"name": "public"}],
            },
        },
    )
    with pytest.raises(ConfigurationError, match="does not permit namespace 'team-a'"):
        await resolve_replication(ctx, obj)


async def test_every_selected_schema_must_be_managed_by_the_source(ctx, k8s) -> None:
    obj = _store(
        k8s,
        _replication(
            "unmanaged",
            "orders",
            "orders-copy",
            schemas=[{"name": "audit"}],
            tables=[{"schema": "billing", "name": "invoices"}],
        ),
    )
    with pytest.raises(ConfigurationError, match=r"billing are not managed by source PostgresDB team-a/orders"):
        await resolve_replication(ctx, obj)


async def test_a_replication_that_closes_a_cycle_is_refused(ctx, k8s) -> None:
    """Not only the direct reverse: any loop republishes changes forever."""
    _store(k8s, _replication("prod-to-reporting", "orders", "orders-copy"))
    _store(k8s, _replication("reporting-to-archive", "orders-copy", "orders-archive"))
    closing = _store(k8s, _replication("archive-to-prod", "orders-archive", "orders"))

    with pytest.raises(ConfigurationError, match="cycle") as raised:
        await resolve_replication(ctx, closing)
    assert "team-a/prod-to-reporting" in str(raised.value)
    assert "team-a/reporting-to-archive" in str(raised.value)


async def test_a_chain_without_a_loop_is_accepted(ctx, k8s) -> None:
    _store(k8s, _replication("prod-to-reporting", "orders", "orders-copy"))
    chained = _store(k8s, _replication("reporting-to-archive", "orders-copy", "orders-archive"))
    await resolve_replication(ctx, chained)


async def test_an_aliased_database_cannot_hide_a_cycle(ctx, k8s) -> None:
    """A second PostgresDB naming the same physical database is the same node."""
    k8s.custom.add(
        _database("team-a", "copy-alias", "reporting", databaseName="orders_copy"), PLURAL_DATABASE
    )
    _store(k8s, _replication("prod-to-reporting", "orders", "orders-copy"))
    back = _store(k8s, _replication("back-via-alias", "copy-alias", "orders"))
    with pytest.raises(ConfigurationError, match="cycle"):
        await resolve_replication(ctx, back)


async def test_a_replication_being_deleted_closes_no_cycle(ctx, k8s) -> None:
    leaving = _replication("prod-to-reporting", "orders", "orders-copy")
    leaving["metadata"]["deletionTimestamp"] = "2026-10-21T00:00:00Z"
    _store(k8s, leaving)
    await resolve_replication(ctx, _store(k8s, _replication("back", "orders-copy", "orders")))


async def test_an_invalid_spec_is_a_configuration_error(ctx, k8s) -> None:
    obj = _store(k8s, _replication("empty", "orders", "orders-copy", schemas=[]))
    with pytest.raises(ConfigurationError, match="at least one entry"):
        await resolve_replication(ctx, obj)


# --- two replications into one target table -------------------------------


def _publishing(name: str, created: str, tables: list[str], **target: Any) -> dict[str, Any]:
    obj = _replication(name, "orders", "orders-copy")
    obj["metadata"]["creationTimestamp"] = created
    obj["status"] = {
        "target": {"instance": "reporting", "database": "orders_copy", **target},
        "publishedTables": tables,
    }
    return obj


async def test_an_older_replication_keeps_a_contested_table(ctx, k8s) -> None:
    from pg_operator.reconcile.replication import _claims_elsewhere

    older = _store(k8s, _publishing("older", "2026-10-01T00:00:00Z", ["public.a", "public.b"]))
    newer = _store(k8s, _publishing("newer", "2026-10-02T00:00:00Z", ["public.b"]))
    resolved = await resolve_replication(ctx, newer)

    claims = await _claims_elsewhere(ctx, newer, resolved.target)
    assert claims == {"public.a": "team-a/older", "public.b": "team-a/older"}
    # The older one defers to nobody, so the two can never both drop a table.
    assert await _claims_elsewhere(ctx, older, resolved.target) == {}


async def test_only_replications_into_the_same_physical_database_claim(ctx, k8s) -> None:
    from pg_operator.reconcile.replication import _claims_elsewhere

    _store(k8s, _publishing("elsewhere", "2026-10-01T00:00:00Z", ["public.a"], database="other"))
    leaving = _publishing("leaving", "2026-10-01T00:00:00Z", ["public.a"])
    leaving["metadata"]["deletionTimestamp"] = "2026-10-03T00:00:00Z"
    _store(k8s, leaving)
    # Same database name, but on another server.
    _store(k8s, _publishing("other-server", "2026-10-01T00:00:00Z", ["public.a"], instance="archive"))
    newer = _store(k8s, _publishing("newer", "2026-10-02T00:00:00Z", []))
    resolved = await resolve_replication(ctx, newer)
    assert await _claims_elsewhere(ctx, newer, resolved.target) == {}


# --- what the target server's connection errors mean ----------------------


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (
            "could not connect to the publisher: connection to server at \"src\" failed: "
            "FATAL:  permission denied to start WAL sender",
            "ReplicationRefused",
        ),
        (
            "could not connect to the publisher: FATAL:  must be superuser or replication role "
            "to start walsender",
            "ReplicationRefused",
        ),
        (
            "could not connect to the publisher: connection to server at \"src\" failed: "
            "Connection timed out",
            "SourceUnreachableFromTarget",
        ),
        (
            "could not connect to the publisher: connection to server at \"src\" failed: "
            "server does not support SSL, but SSL was required",
            "SourceUnreachableFromTarget",
        ),
        ("some other failure", "ReconcileFailure"),
    ],
)
async def test_connection_errors_name_their_cause(message: str, expected: str) -> None:
    from pg_operator.errors import ReconcileFailure
    from pg_operator.reconcile.replication import ReplicationEnd, _explain

    end = ReplicationEnd("team-a", "orders", "prod", "orders", "prod.rds", 5432)
    result = ReplicationResult(source=end, target=end, names=replication_names("team-a", "x"), login="l")
    explained = _explain(ReconcileFailure(message), result, "prod.internal", 5432)
    assert type(explained).__name__ == expected
    if expected == "SourceUnreachableFromTarget":
        assert "prod.internal:5432" in str(explained) and "source.connection" in str(explained)
        # A TLS mismatch names sslMode, not the network.
        assert ("sslMode" in str(explained)) == ("SSL" in message)
        assert ("security groups" in str(explained)) == ("SSL" not in message)
