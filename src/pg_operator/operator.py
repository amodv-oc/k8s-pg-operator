"""Entrypoint module for ``kopf run``.

    kopf run --module pg_operator.operator --all-namespaces

Importing this module registers the startup, reconcile and finalizer handlers
for all three custom resources.
"""

from __future__ import annotations

# Imported for its registration side effects; re-exported so the import is
# not mistaken for a stray one.
from . import handlers

__all__ = ["handlers"]
