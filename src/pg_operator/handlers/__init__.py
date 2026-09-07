"""Handler registration.

Importing this package registers every kopf handler. ``kopf run`` is pointed at
``pg_operator.operator``, which imports this module.
"""

from __future__ import annotations

from . import database, instance, startup, user

__all__ = ["database", "instance", "startup", "user"]
