"""Structured logging configuration shared by the operator and the API."""

from __future__ import annotations

import logging
import sys

from pythonjsonlogger.json import JsonFormatter

_CONFIGURED = False

_QUIET_LOGGERS = (
    "kubernetes_asyncio.client.rest",
    "urllib3.connectionpool",
    "asyncio",
)


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    """Install a single stdout handler with either JSON or console formatting."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    handler = logging.StreamHandler(sys.stdout)
    if fmt == "json":
        handler.setFormatter(
            JsonFormatter(
                "{levelname}{name}{message}",
                style="{",
                rename_fields={"levelname": "level", "name": "logger", "message": "msg"},
                timestamp=True,
            )
        )
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
