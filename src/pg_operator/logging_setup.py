"""Structured logging configuration shared by the operator and the API."""

from __future__ import annotations

import logging
import sys
from typing import Any

from pythonjsonlogger.core import RESERVED_ATTRS
from pythonjsonlogger.json import JsonEncoder, JsonFormatter

_CONFIGURED = False

_QUIET_LOGGERS = (
    "kubernetes_asyncio.client.rest",
    "urllib3.connectionpool",
    "asyncio",
)

#: Attributes kopf attaches to every record logged through its per-object
#: loggers. ``settings`` is the live ``OperatorSettings`` dataclass, which holds
#: a ``SimpleQueue`` for event posting: python-json-logger renders dataclasses
#: with ``dataclasses.asdict``, which deep-copies, and a queue cannot be copied.
#: Left in, that made *every* kopf-emitted line fail to format in JSON mode,
#: so the operator's own progress messages never reached stdout. kopf's own
#: JSON formatter drops the same three keys.
_KOPF_INTERNAL_ATTRS = ("settings", "k8s_ref", "k8s_skip")

_ENCODER = JsonEncoder()


def _json_default(obj: Any) -> Any:
    """Encode what the library knows how to; never let an odd extra lose a line.

    The library's encoder handles datetimes, dataclasses, exceptions and the
    like. Anything it cannot turn into JSON is rendered as its ``repr`` rather
    than raising, because a log line that fails to format is reported to stderr
    as a traceback and the message itself is dropped.
    """
    try:
        return _ENCODER.default(obj)
    except Exception:  # noqa: BLE001 - a log formatter must not raise
        return repr(obj)


def json_formatter() -> JsonFormatter:
    """The JSON formatter used for every log line in ``json`` mode."""
    return JsonFormatter(
        "{levelname}{name}{message}",
        style="{",
        rename_fields={"levelname": "level", "name": "logger", "message": "msg"},
        reserved_attrs=[*RESERVED_ATTRS, *_KOPF_INTERNAL_ATTRS],
        json_default=_json_default,
        timestamp=True,
    )


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    """Install a single stdout handler with either JSON or console formatting."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    handler = logging.StreamHandler(sys.stdout)
    if fmt == "json":
        handler.setFormatter(json_formatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(name)-32s %(message)s")
        )

    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    for name in _QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    _CONFIGURED = True
