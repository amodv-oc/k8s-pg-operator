"""PostgresReplication's first reconcile step, and the status it all produces.

Step 1 - resolve both ends, refuse the impossible - never connects to a server,
so it is tested here in full against a fake cluster. The later steps need real
servers and live in tests/integration/test_replication_source.py.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
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
    """Store ``obj``, stamped - as the API server stamps it - later than everything stored before."""
    created = datetime(2026, 10, 1, tzinfo=UTC) + timedelta(minutes=len(k8s.custom.objects))
    obj["metadata"].setdefault("creationTimestamp", created.strftime("%Y-%m-%dT%H:%M:%SZ"))
    k8s.custom.add(obj, PLURAL_REPLICATION)
    return obj


async def test_both_ends_resolve(ctx, k8s) -> None:
    obj = _store(k8s, _replication("to-reporting", "orders", "orders-copy", schemas=[{"name": "audit"}]))
    resolved = await resolve_replication(ctx, obj)
    assert resolved.source.display == "prod/orders"
    assert resolved.target.display == "reporting/orders_copy"
    assert resolved.source.as_status()["postgresDB"] == "team-a/orders"
    assert resolved.source_instance.host == "prod.rds"


def _with_status(obj: dict[str, Any], **ends: str) -> dict[str, Any]:
    """Status as a pass that created objects on ``ends`` (side -> instance/database) leaves it."""
    status: dict[str, Any] = {"created": {"publication": True, "slot": True}}
    for side, end in ends.items():
        instance, database = end.split("/")
        status[side] = {"instance": instance, "database": database}
    obj["status"] = status
    return obj


async def test_an_end_that_moved_under_its_objects_is_refused(ctx, k8s) -> None:
    """Converging the new end would orphan the old slot, which teardown finds only through status."""
    obj = _store(k8s, _with_status(
        _replication("to-reporting", "orders", "orders-copy"),
        source="old-prod/orders",
        target="reporting/orders_copy",
    ))
    with pytest.raises(ConfigurationError, match=r"now resolves to prod/orders.*old-prod/orders"):
        await resolve_replication(ctx, obj)


async def test_a_moved_target_database_is_refused(ctx, k8s) -> None:
    obj = _store(k8s, _with_status(
        _replication("to-reporting", "orders", "orders-copy"),
        source="prod/orders",
        target="reporting/orders_copy_v1",
    ))
    with pytest.raises(ConfigurationError, match="target PostgresDB team-a/orders-copy"):
        await resolve_replication(ctx, obj)


async def test_ends_matching_status_resolve(ctx, k8s) -> None:
    obj = _store(k8s, _with_status(
        _replication("to-reporting", "orders", "orders-copy"),
        source="prod/orders",
        target="reporting/orders_copy",
    ))
    assert (await resolve_replication(ctx, obj)).source.display == "prod/orders"


async def test_an_end_may_move_before_anything_was_created(ctx, k8s) -> None:
    """Nothing to orphan yet - e.g. every pass so far failed before the publication."""
    obj = _with_status(_replication("to-reporting", "orders", "orders-copy"), source="old-prod/orders")
    obj["status"]["created"] = {}
    _store(k8s, obj)
    assert (await resolve_replication(ctx, obj)).source.display == "prod/orders"


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


async def test_recording_one_object_never_blanks_another(ctx, k8s) -> None:
    """Status is a merge patch: an empty login or a None oid would erase what an earlier pass recorded.

    Teardown works from status alone, so a blanked login is a login it never
    drops, and a lost oid hides the table-sync slots it names.
    """
    from pg_operator.reconcile.replication import _record_created

    obj = _store(k8s, _replication("to-reporting", "orders", "orders-copy"))
    obj["status"] = {
        "source": {"login": "pgop_repl_x"},
        "target": {"subscriptionOid": 16400},
        "created": {"login": True, "subscription": True},
    }
    resolved = await resolve_replication(ctx, obj)
    # A pass that has recreated the publication and knows nothing else yet.
    result = ReplicationResult(
        source=resolved.source,
        target=resolved.target,
        names=replication_names("team-a", "to-reporting"),
        created={"publication": True},
    )
    await _record_created(ctx, obj, result)
    stored = k8s.custom.get_stored(PLURAL_REPLICATION, "team-a", "to-reporting")["status"]
    assert stored["source"]["login"] == "pgop_repl_x"
    assert stored["target"]["subscriptionOid"] == 16400
    assert stored["created"] == {"publication": True, "login": True, "subscription": True}


async def test_a_replaced_login_stays_known_until_it_is_removed(ctx, k8s) -> None:
    """What teardown needs to remove the old login moves with it, in the same record as the new one."""
    from pg_operator.reconcile.replication import _take_login

    result = await _result(
        ctx, k8s,
        credentials={"namespace": "pg-operator", "name": "pgop-repl.team-a.x", "generated": True},
        created={"publication": True, "login": True, "secret": True},
        granted=["rds_replication", "orders_ro"],
    )
    provided = {"namespace": "team-a", "name": "byo", "generated": False}
    _take_login(result, "byo_login", credentials=provided)
    assert result.superseded == [
        {
            "login": "pgop_repl_x",
            "created": True,
            "grantedMemberships": ["orders_ro", "rds_replication"],
            "secret": {"namespace": "pg-operator", "name": "pgop-repl.team-a.x"},
        }
    ]
    # The new login starts with nothing the operator granted, and is not the operator's.
    assert (result.login, result.granted) == ("byo_login", [])
    assert result.created == {"publication": True, "login": False, "secret": False}

    # Put back before the swap finished: the generated login is current again,
    # with its grants, and the provided one - never the operator's, so no
    # Secret - is the one waiting to be removed.
    result.granted.append("orders_ro")
    _take_login(
        result, "pgop_repl_x",
        credentials={"namespace": "pg-operator", "name": "pgop-repl.team-a.x", "generated": True},
    )
    assert result.superseded == [
        {"login": "byo_login", "created": False, "grantedMemberships": ["orders_ro"]}
    ]
    assert sorted(result.granted) == ["orders_ro", "rds_replication"]
    assert result.created["login"] is True and result.created["secret"] is True


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
    first = _store(k8s, _replication("prod-to-reporting", "orders", "orders-copy"))
    second = _store(k8s, _replication("reporting-to-archive", "orders-copy", "orders-archive"))
    closing = _store(k8s, _replication("archive-to-prod", "orders-archive", "orders"))

    with pytest.raises(ConfigurationError, match="cycle") as raised:
        await resolve_replication(ctx, closing)
    assert "team-a/prod-to-reporting" in str(raised.value)
    assert "team-a/reporting-to-archive" in str(raised.value)
    # Only where the ring closes: the two before it go on.
    for earlier in (first, second):
        await resolve_replication(ctx, earlier)


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


def _streaming(obj: dict[str, Any], *, source: str, target: str) -> dict[str, Any]:
    """Status as a replication whose subscription exists leaves it."""
    _with_status(obj, source=source, target=target)
    obj["status"]["created"]["subscription"] = True
    return obj


async def test_a_new_reverse_replication_does_not_stop_the_streaming_one(ctx, k8s) -> None:
    """The newcomer is the one that closes the loop, so it alone is refused.

    Refusing the streaming one would stop its convergence - new tables, status,
    and D6's unpublish of a table that lost its replica identity - while its
    subscription streams on regardless.
    """
    streaming = _store(k8s, _streaming(
        _replication("prod-to-reporting", "orders", "orders-copy"),
        source="prod/orders",
        target="reporting/orders_copy",
    ))
    newcomer = _store(k8s, _replication("newcomer", "orders-copy", "orders"))

    await resolve_replication(ctx, streaming)
    with pytest.raises(ConfigurationError, match="cycle with team-a/prod-to-reporting"):
        await resolve_replication(ctx, newcomer)


async def test_an_older_replication_yields_to_a_younger_one_already_streaming(ctx, k8s) -> None:
    """Say the older one could not resolve until the younger was streaming.

    Were age alone to decide, the older would now start replicating round a live loop.
    """
    older = _replication("prod-to-reporting", "orders", "orders-copy")
    older["metadata"]["creationTimestamp"] = "2026-09-01T00:00:00Z"
    _store(k8s, older)
    younger = _store(k8s, _streaming(
        _replication("reporting-to-prod", "orders-copy", "orders"),
        source="reporting/orders_copy",
        target="prod/orders",
    ))

    await resolve_replication(ctx, younger)
    with pytest.raises(ConfigurationError, match="cycle with team-a/reporting-to-prod"):
        await resolve_replication(ctx, older)


async def test_of_two_new_replications_in_a_loop_only_the_younger_is_refused(ctx, k8s) -> None:
    """Created in the same second, so namespace and name rank them.

    Their passes run concurrently and neither has created anything, so each
    must reach the same verdict on its own: neither may defer to the other, nor
    both ignore it.
    """
    same_second = "2026-10-05T00:00:00Z"
    forward = _replication("a-forward", "orders", "orders-copy")
    back = _replication("b-back", "orders-copy", "orders")
    for obj in (back, forward):
        obj["metadata"]["creationTimestamp"] = same_second
        _store(k8s, obj)

    await resolve_replication(ctx, forward)
    with pytest.raises(ConfigurationError, match="cycle with team-a/a-forward"):
        await resolve_replication(ctx, back)


async def test_two_streaming_replications_in_a_loop_both_report_it(ctx, k8s) -> None:
    """A live loop: whichever came first, it is real, and each side says so."""
    forward = _store(k8s, _streaming(
        _replication("forward", "orders", "orders-copy"), source="prod/orders", target="reporting/orders_copy"
    ))
    back = _store(k8s, _streaming(
        _replication("back", "orders-copy", "orders"), source="reporting/orders_copy", target="prod/orders"
    ))
    with pytest.raises(ConfigurationError, match="cycle with team-a/back"):
        await resolve_replication(ctx, forward)
    with pytest.raises(ConfigurationError, match="cycle with team-a/forward"):
        await resolve_replication(ctx, back)


async def test_a_streaming_replication_counts_where_status_puts_it(ctx, k8s) -> None:
    """Its subscription streams whatever has become of its PostgresDBs.

    Here its target PostgresDB was deleted under RETAIN - the database and the
    subscription in it stay - so its spec no longer resolves.
    """
    k8s.custom.add(
        _database("team-a", "copy-alias", "reporting", databaseName="orders_copy"), PLURAL_DATABASE
    )
    _store(k8s, _streaming(
        _replication("prod-to-reporting", "orders", "orders-copy"),
        source="prod/orders",
        target="reporting/orders_copy",
    ))
    del k8s.custom.objects[(PLURAL_DATABASE, "team-a", "orders-copy")]

    back = _store(k8s, _replication("back", "copy-alias", "orders"))
    with pytest.raises(ConfigurationError, match="cycle with team-a/prod-to-reporting"):
        await resolve_replication(ctx, back)


async def test_a_streaming_replication_being_deleted_closes_a_cycle_until_it_is_gone(ctx, k8s) -> None:
    """Its subscription lives until its teardown removes it, and the object goes only after that.

    Deleting a replication and creating its reverse at once - swapping the
    direction - must not run both subscriptions side by side.
    """
    leaving = _streaming(
        _replication("prod-to-reporting", "orders", "orders-copy"),
        source="prod/orders",
        target="reporting/orders_copy",
    )
    leaving["metadata"]["deletionTimestamp"] = "2026-10-21T00:00:00Z"
    _store(k8s, leaving)
    back = _store(k8s, _replication("back", "orders-copy", "orders"))
    with pytest.raises(ConfigurationError, match="cycle with team-a/prod-to-reporting"):
        await resolve_replication(ctx, back)

    del k8s.custom.objects[(PLURAL_REPLICATION, "team-a", "prod-to-reporting")]
    await resolve_replication(ctx, back)


async def test_another_replication_that_cannot_resolve_adds_no_edge(ctx, k8s) -> None:
    """Older, not yet streaming, and unable to resolve: it cannot be replicating, and does not fail this pass."""
    k8s.custom.add(_database("team-a", "orphan", "no-such-instance"), PLURAL_DATABASE)
    k8s.custom.add(_instance("broken", "broken.rds", port="not-a-port"), PLURAL_INSTANCE)
    k8s.custom.add(_database("team-a", "on-broken", "broken"), PLURAL_DATABASE)
    for name, source, target, extra in (
        ("missing-db", "not-yet", "orders", {}),
        ("missing-instance", "orphan", "orders", {}),
        ("invalid-instance", "on-broken", "orders", {}),
        ("invalid-spec", "orders-copy", "orders", {"schemas": []}),
    ):
        other = _replication(name, source, target, **extra)
        other["metadata"]["creationTimestamp"] = "2026-09-01T00:00:00Z"
        _store(k8s, other)
    await resolve_replication(ctx, _store(k8s, _replication("to-reporting", "orders", "orders-copy")))


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

    claims = await _claims_elsewhere(resolved.peers, newer, resolved.target)
    assert claims == {"public.a": "team-a/older", "public.b": "team-a/older"}
    # The older one defers to nobody, so the two can never both drop a table.
    resolved = await resolve_replication(ctx, older)
    assert await _claims_elsewhere(resolved.peers, older, resolved.target) == {}


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
    assert await _claims_elsewhere(resolved.peers, newer, resolved.target) == {}


# --- what a pass costs the Kubernetes API -----------------------------------


def _counting_reads(k8s: FakeK8sClient, monkeypatch: pytest.MonkeyPatch) -> Counter[str]:
    reads: Counter[str] = Counter()
    for method in (
        "get_cluster_custom_object",
        "get_namespaced_custom_object",
        "list_cluster_custom_object",
        "list_namespaced_custom_object",
    ):
        real = getattr(k8s.custom, method)

        async def counted(*args: Any, _real: Any = real, _method: str = method, **kwargs: Any) -> Any:
            reads[_method] += 1
            return await _real(*args, **kwargs)

        monkeypatch.setattr(k8s.custom, method, counted)
    return reads


async def _reads_in_a_pass(ctx, reads: Counter[str], obj: dict[str, Any]) -> Counter[str]:
    """Kubernetes reads in the two steps that look at other replications: resolving, and claims."""
    from pg_operator.reconcile.replication import _claims_elsewhere

    reads.clear()
    resolved = await resolve_replication(ctx, obj)
    await _claims_elsewhere(resolved.peers, obj, resolved.target)
    return Counter(reads)


def _another(k8s: FakeK8sClient, index: int) -> None:
    """One more replication elsewhere in the cluster, alternately streaming and not yet.

    Each is older than the one measured, so every one of them is looked at, and
    none closes a loop with it.
    """
    created = f"2026-09-01T00:{index // 60:02d}:{index % 60:02d}Z"
    if index % 2:
        other = _publishing(f"into-copy-{index}", created, ["public.a"])
        _streaming(other, source="prod/orders", target="reporting/orders_copy")
    else:
        other = _replication(f"to-archive-{index}", "orders-copy", "orders-archive")
        other["metadata"]["creationTimestamp"] = created
    _store(k8s, other)


async def test_reads_per_pass_do_not_grow_with_other_replications(ctx, k8s, monkeypatch) -> None:
    """Every replication runs a pass a minute; one read per other replication is quadratic cluster-wide."""
    reads = _counting_reads(k8s, monkeypatch)
    mine = _store(k8s, _replication("to-reporting", "orders", "orders-copy"))
    alone = await _reads_in_a_pass(ctx, reads, mine)
    # Its own two ends, and one list that finds nobody else - so nothing else is read.
    assert alone == {"get_namespaced_custom_object": 2, "get_cluster_custom_object": 2, "list_cluster_custom_object": 1}

    costs = []
    for others in (2, 30):
        while sum(1 for key in k8s.custom.objects if key[0] == PLURAL_REPLICATION) <= others:
            _another(k8s, len(k8s.custom.objects))
        costs.append(await _reads_in_a_pass(ctx, reads, mine))
    assert costs[0] == costs[1], costs
    # Beyond that, a list of the PostgresDBs and one of the PostgresInstances.
    assert costs[1] - alone == {"list_cluster_custom_object": 2}


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
