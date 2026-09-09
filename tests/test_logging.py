"""JSON log formatting must survive whatever kopf attaches to a record."""

from __future__ import annotations

import json
import logging
import queue
from datetime import UTC, datetime

import kopf

from pg_operator.logging_setup import json_formatter


def _record(message: str, **extra: object) -> logging.LogRecord:
    record = logging.LogRecord("pg_operator.test", logging.INFO, "f.py", 1, message, None, None)
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_kopf_object_logger_extras_do_not_break_formatting() -> None:
    """kopf attaches its live OperatorSettings, which holds an uncopyable queue.

    python-json-logger renders dataclasses with ``dataclasses.asdict``, which
    deep-copies; the queue inside made every kopf-emitted line fail to format
    and be dropped. Those attributes are kopf's plumbing, not log content.
    """
    record = _record(
        "Timer 'reconcile_periodically' succeeded.",
        settings=kopf.OperatorSettings(),
        k8s_ref={"apiVersion": "v1alpha1", "kind": "PostgresDB", "name": "orders"},
        k8s_skip=False,
    )

    payload = json.loads(json_formatter().format(record))

    assert payload["msg"] == "Timer 'reconcile_periodically' succeeded."
    assert payload["level"] == "INFO"
    assert payload["logger"] == "pg_operator.test"
    for internal in ("settings", "k8s_ref", "k8s_skip"):
        assert internal not in payload


def test_unserialisable_extra_is_rendered_not_fatal() -> None:
    """An odd extra must degrade to its repr, never lose the line."""
    record = _record("hello", oddity=queue.SimpleQueue(), when=datetime(2026, 1, 1, tzinfo=UTC))

    payload = json.loads(json_formatter().format(record))

    assert payload["msg"] == "hello"
    assert "SimpleQueue" in payload["oddity"]
    # Types the library understands are still rendered properly.
    assert payload["when"].startswith("2026-01-01")


def test_own_extras_still_appear() -> None:
    record = _record("pool recycled", instance="prod", database="orders")
    payload = json.loads(json_formatter().format(record))
    assert payload["instance"] == "prod"
    assert payload["database"] == "orders"
