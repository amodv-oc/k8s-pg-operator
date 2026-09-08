"""Handler plumbing: how failures map onto kopf's retry behaviour."""

from __future__ import annotations

import kopf
import pytest
from kubernetes_asyncio.client.exceptions import ApiException

from pg_operator.errors import (
    ConfigurationError,
    ConnectionFailure,
    OperatorError,
    ReconcileFailure,
    ReferenceNotFound,
)
from pg_operator.handlers.common import as_kopf_error, is_paused


def test_configuration_error_is_permanent() -> None:
    """A bad spec cannot succeed on retry; retrying would just burn cycles."""
    error = as_kopf_error(ConfigurationError("schemas contains duplicate names"))
    assert isinstance(error, kopf.PermanentError)
    assert "duplicate names" in str(error)


def test_missing_reference_is_temporary() -> None:
    """Ordering: a PostgresUser may be applied before its PostgresDB exists."""
    error = as_kopf_error(ReferenceNotFound("PostgresDB team-a/orders not found"))
    assert isinstance(error, kopf.TemporaryError)
    assert error.delay is not None and error.delay > 0


def test_connection_failure_is_temporary() -> None:
    error = as_kopf_error(ConnectionFailure("timeout connecting to prod.rds:5432"))
    assert isinstance(error, kopf.TemporaryError)


def test_reconcile_failure_is_temporary() -> None:
    error = as_kopf_error(ReconcileFailure("GRANT failed: deadlock detected"))
    assert isinstance(error, kopf.TemporaryError)


def test_conflict_retries_quickly() -> None:
    """A concurrent write should be re-read soon, not after the full backoff."""
    error = as_kopf_error(ApiException(status=409, reason="Conflict"))
    assert isinstance(error, kopf.TemporaryError)
    assert error.delay == 5


def test_unexpected_errors_are_temporary_not_swallowed() -> None:
    error = as_kopf_error(RuntimeError("something odd"))
    assert isinstance(error, kopf.TemporaryError)
    assert "something odd" in str(error)


def test_an_error_with_no_message_still_reports_its_type() -> None:
    error = as_kopf_error(RuntimeError())
    assert "RuntimeError" in str(error)


def test_operator_error_base_is_temporary() -> None:
    assert isinstance(as_kopf_error(OperatorError("generic")), kopf.TemporaryError)


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"spec": {"paused": True}}, True),
        ({"spec": {"paused": False}}, False),
        ({"spec": {}}, False),
        ({}, False),
    ],
)
def test_is_paused(body: dict, expected: bool) -> None:
    assert is_paused(body) is expected  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# run_reconcile / run_teardown against a fake cluster
# ---------------------------------------------------------------------------

import asyncio  # noqa: E402
import logging  # noqa: E402
from typing import Any  # noqa: E402

from pg_operator.config import Settings  # noqa: E402
from pg_operator.constants import PLURAL_DATABASE  # noqa: E402
from pg_operator.errors import InstanceNotFound  # noqa: E402
from pg_operator.handlers.common import (  # noqa: E402
    RESOURCE_READY,
    run_reconcile,
    run_teardown,
)
from pg_operator.reconcile.context import ReconcileContext, holder  # noqa: E402

from .fakes import FakeK8sClient  # noqa: E402

_log = logging.getLogger("test")


@pytest.fixture
def cluster():
    """A fake cluster installed as the process-wide reconcile context."""
    k8s = FakeK8sClient()
    holder.set(ReconcileContext(k8s, settings=Settings()))  # type: ignore[arg-type]
    try:
        yield k8s
    finally:
        holder.clear()


def _db(name: str = "orders", **status: Any) -> dict[str, Any]:
    return {
        "apiVersion": "postgres.ourcommunity.com.au/v1alpha1",
        "kind": "PostgresDB",
        "metadata": {"name": name, "namespace": "team-a", "generation": 3},
        "spec": {"instanceRef": {"name": "prod"}},
        "status": status,
    }


def _conditions(obj: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {c["type"]: c for c in obj["status"]["conditions"]}


async def test_connection_failure_marks_the_resource_unreachable(cluster: FakeK8sClient) -> None:
    """A lost server must not leave `Reachable: True` from the last good pass.

    Otherwise the phase reads Failed and sends the reader hunting for a spec
    problem when the network or the credentials are what changed.
    """
    body = _db(
        conditions=[
            {"type": "Ready", "status": "True", "reason": "Reconciled", "message": "ok"},
            {"type": "Reachable", "status": "True", "reason": "Connected", "message": "ok"},
        ]
    )
    cluster.custom.add(body, PLURAL_DATABASE)

    async def failing() -> tuple[str, dict[str, Any]]:
        raise ConnectionFailure("cannot connect to prod:5432/postgres: timeout")

    with pytest.raises(kopf.TemporaryError):
        await run_reconcile(
            kind="PostgresDB", plural=PLURAL_DATABASE, body=body, logger=_log, reconcile=failing
        )

    stored = cluster.custom.get_stored(PLURAL_DATABASE, "team-a", "orders")
    assert stored is not None
    conditions = _conditions(stored)
    assert conditions["Reachable"]["status"] == "False"
    assert conditions["Ready"]["status"] == "False"
    assert conditions["Ready"]["reason"] == "ConnectionFailed"
    assert stored["status"]["phase"] == "Unreachable"


async def test_a_non_connectivity_failure_keeps_reachable_intact(cluster: FakeK8sClient) -> None:
    body = _db(
        conditions=[
            {"type": "Reachable", "status": "True", "reason": "Connected", "message": "ok"},
        ]
    )
    cluster.custom.add(body, PLURAL_DATABASE)

    async def failing() -> tuple[str, dict[str, Any]]:
        raise ReconcileFailure("GRANT failed")

    with pytest.raises(kopf.TemporaryError):
        await run_reconcile(
            kind="PostgresDB", plural=PLURAL_DATABASE, body=body, logger=_log, reconcile=failing
        )

    stored = cluster.custom.get_stored(PLURAL_DATABASE, "team-a", "orders")
    assert stored is not None
    assert _conditions(stored)["Reachable"]["status"] == "True"
    assert stored["status"]["phase"] == "Failed"


async def test_concurrent_passes_over_one_resource_are_serialised(cluster: FakeK8sClient) -> None:
    """The periodic timer and the change handler are independent kopf tasks."""
    body = _db()
    cluster.custom.add(body, PLURAL_DATABASE)
    active = 0
    peak = 0

    async def slow() -> tuple[str, dict[str, Any]]:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return "ok", {"phase": "Ready", "conditions": []}

    await asyncio.gather(
        *(
            run_reconcile(
                kind="PostgresDB", plural=PLURAL_DATABASE, body=body, logger=_log, reconcile=slow
            )
            for _ in range(4)
        )
    )
    assert peak == 1


async def test_teardown_releases_the_finalizer_when_the_instance_is_gone(cluster) -> None:
    async def teardown() -> str:
        raise InstanceNotFound("PostgresInstance 'prod' not found")

    message = await run_teardown(kind="PostgresDB", body=_db(), logger=_log, teardown=teardown)
    assert message.startswith("cleanup skipped")


async def test_teardown_retries_when_only_a_secret_is_missing(cluster) -> None:
    """A missing credentials Secret is temporary; dropping the finalizer over it
    would leave a DROP-policy role live on the server with nothing tracking it."""

    async def teardown() -> str:
        raise ReferenceNotFound("Secret pg-operator/prod-super not found")

    with pytest.raises(kopf.TemporaryError):
        await run_teardown(kind="PostgresDB", body=_db(), logger=_log, teardown=teardown)


async def test_teardown_releases_the_finalizer_on_a_bad_spec(cluster) -> None:
    async def teardown() -> str:
        raise ConfigurationError("invalid spec: derived group role names collide")

    message = await run_teardown(kind="PostgresDB", body=_db(), logger=_log, teardown=teardown)
    assert message.startswith("cleanup skipped")


def _ready_gauge_labels() -> set[tuple[str, str, str]]:
    return {
        (s.labels["kind"], s.labels["namespace"], s.labels["name"])
        for metric in RESOURCE_READY.collect()
        for s in metric.samples
    }


async def test_teardown_forgets_the_resource_gauge(cluster) -> None:
    """Deleted resources must not stay in the metrics output forever."""
    RESOURCE_READY.labels("PostgresDB", "team-a", "gone").set(1)
    assert ("PostgresDB", "team-a", "gone") in _ready_gauge_labels()

    async def teardown() -> str:
        return "dropped"

    await run_teardown(
        kind="PostgresDB", body=_db(name="gone"), logger=_log, teardown=teardown
    )
    assert ("PostgresDB", "team-a", "gone") not in _ready_gauge_labels()
