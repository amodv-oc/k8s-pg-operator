"""Run the operator without the ``kopf`` CLI.

``python -m pg_operator`` is equivalent to ``kopf run --module
pg_operator.operator``, with the watch scope taken from
``PG_OPERATOR_WATCH_NAMESPACES``. The container image uses this so the scope is
configured in exactly one place.
"""

from __future__ import annotations

import sys

import kopf

from . import operator  # noqa: F401 - registers handlers
from .config import get_settings
from .logging_setup import configure_logging

#: Bound on all interfaces so the kubelet can reach the probe.
_LIVENESS_ENDPOINT = "http://0.0.0.0:8080/healthz"


def main() -> int:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)

    namespaces = settings.watch_namespaces
    kopf.configure(verbose=settings.log_level == "DEBUG", log_prefix=False)
    # An empty watch scope means cluster-wide. Cluster-scoped resources such as
    # PostgresInstance are watched cluster-wide either way.
    kopf.run(
        namespaces=namespaces,
        clusterwide=not namespaces,
        liveness_endpoint=_LIVENESS_ENDPOINT,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
