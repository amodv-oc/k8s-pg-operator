"""Run the state API with uvicorn.

``python -m pg_operator.api`` (or the ``pg-operator-api`` console script) is
what the API container runs, so host, port and log format come from the same
environment configuration as the operator.
"""

from __future__ import annotations

import sys

import uvicorn

from ..config import get_settings
from ..logging_setup import configure_logging


def main() -> int:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    uvicorn.run(
        "pg_operator.api.main:app",
        host=settings.api_host,
        port=settings.api_port,
        log_config=None,
        access_log=settings.log_level == "DEBUG",
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
