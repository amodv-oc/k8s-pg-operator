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
