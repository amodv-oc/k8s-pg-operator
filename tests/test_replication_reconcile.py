"""PostgresReplication's first reconcile step: resolve both ends, refuse the impossible.

It never connects to a server, so it is tested here in full against a fake
cluster.
"""

from __future__ import annotations

from typing import Any

import pytest

from pg_operator.config import Settings
from pg_operator.constants import PLURAL_DATABASE, PLURAL_INSTANCE, PLURAL_REPLICATION
from pg_operator.errors import ConfigurationError, ReferenceNotFound
from pg_operator.reconcile.context import ReconcileContext
from pg_operator.reconcile.replication import build_status, reconcile_replication

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


async def test_both_ends_resolve_and_the_replication_is_pending(ctx, k8s) -> None:
    obj = _store(k8s, _replication("to-reporting", "orders", "orders-copy", schemas=[{"name": "audit"}]))
    result = await reconcile_replication(ctx, obj)

    assert result.source.display == "prod/orders"
    assert result.target.display == "reporting/orders_copy"
    status = build_status(result, None, 2)
    assert status["phase"] == "Pending"
    (ready,) = status["conditions"]
    assert (ready["type"], ready["status"], ready["reason"]) == ("Ready", "Unknown", "AwaitingReconciler")
    assert status["source"]["postgresDB"] == "team-a/orders"
    assert status["source"]["publication"].startswith("pgop_team_a_to_reporting_")
    assert status["source"]["slot"] == status["target"]["subscription"]
    assert status["source"]["login"].startswith("pgop_repl_team_a_to_reporting_")


async def test_pending_replaces_a_failure_from_an_earlier_pass(ctx, k8s) -> None:
    """Once the spec is fixed, a Ready=False left by the failed pass must go."""
    obj = _store(k8s, _replication("to-reporting", "orders", "orders-copy"))
    earlier = {"conditions": [{"type": "Ready", "status": "False", "reason": "Configuration", "message": "x"}]}
    status = build_status(await reconcile_replication(ctx, obj), earlier, 2)
    assert status["phase"] == "Pending"


async def test_a_missing_target_is_temporary_and_named(ctx, k8s) -> None:
    obj = _store(k8s, _replication("to-nowhere", "orders", "not-yet"))
    with pytest.raises(ReferenceNotFound, match=r"spec\.target"):
        await reconcile_replication(ctx, obj)


async def test_two_instances_on_one_server_are_one_server(ctx, k8s) -> None:
    """A second PostgresInstance pointing at the same host is not a second server."""
    k8s.custom.add(_instance("prod-alias", "PROD.rds"), PLURAL_INSTANCE)
    k8s.custom.add(_database("team-a", "orders-elsewhere", "prod-alias"), PLURAL_DATABASE)
    obj = _store(k8s, _replication("loopback", "orders", "orders-elsewhere"))
    with pytest.raises(ConfigurationError, match="same server"):
        await reconcile_replication(ctx, obj)


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
        await reconcile_replication(ctx, obj)


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
        await reconcile_replication(ctx, obj)


async def test_a_replication_that_closes_a_cycle_is_refused(ctx, k8s) -> None:
    """Not only the direct reverse: any loop republishes changes forever."""
    _store(k8s, _replication("prod-to-reporting", "orders", "orders-copy"))
    _store(k8s, _replication("reporting-to-archive", "orders-copy", "orders-archive"))
    closing = _store(k8s, _replication("archive-to-prod", "orders-archive", "orders"))

    with pytest.raises(ConfigurationError, match="cycle") as raised:
        await reconcile_replication(ctx, closing)
    assert "team-a/prod-to-reporting" in str(raised.value)
    assert "team-a/reporting-to-archive" in str(raised.value)


async def test_a_chain_without_a_loop_is_accepted(ctx, k8s) -> None:
    _store(k8s, _replication("prod-to-reporting", "orders", "orders-copy"))
    chained = _store(k8s, _replication("reporting-to-archive", "orders-copy", "orders-archive"))
    await reconcile_replication(ctx, chained)


async def test_an_aliased_database_cannot_hide_a_cycle(ctx, k8s) -> None:
    """A second PostgresDB naming the same physical database is the same node."""
    k8s.custom.add(
        _database("team-a", "copy-alias", "reporting", databaseName="orders_copy"), PLURAL_DATABASE
    )
    _store(k8s, _replication("prod-to-reporting", "orders", "orders-copy"))
    back = _store(k8s, _replication("back-via-alias", "copy-alias", "orders"))
    with pytest.raises(ConfigurationError, match="cycle"):
        await reconcile_replication(ctx, back)


async def test_a_replication_being_deleted_closes_no_cycle(ctx, k8s) -> None:
    leaving = _replication("prod-to-reporting", "orders", "orders-copy")
    leaving["metadata"]["deletionTimestamp"] = "2026-10-21T00:00:00Z"
    _store(k8s, leaving)
    await reconcile_replication(ctx, _store(k8s, _replication("back", "orders-copy", "orders")))


async def test_an_invalid_spec_is_a_configuration_error(ctx, k8s) -> None:
    obj = _store(k8s, _replication("empty", "orders", "orders-copy", schemas=[]))
    with pytest.raises(ConfigurationError, match="at least one entry"):
        await reconcile_replication(ctx, obj)
