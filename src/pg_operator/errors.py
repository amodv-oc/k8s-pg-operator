"""Operator error hierarchy.

`TemporaryError` maps onto kopf's retry-with-backoff behaviour; `PermanentError`
means the resource is misconfigured and retrying will not help until the spec or
a referenced object changes.
"""

from __future__ import annotations


class OperatorError(Exception):
    """Base class for all operator errors."""


class ConfigurationError(OperatorError):
    """The resource spec (or a referenced object) is invalid."""


class ReferenceNotFound(ConfigurationError):
    """A referenced Kubernetes object does not exist."""


class InstanceNotFound(ReferenceNotFound):
    """The PostgresInstance a resource points at does not exist.

    Kept apart from a missing Secret or PostgresDB because a finalizer treats
    the two differently: with the instance gone there is nothing left to clean
    up and the delete should complete, whereas a Secret that has gone missing
    is a temporary condition to retry - dropping the finalizer there would leave
    a DROP-policy role live on the server with nothing tracking it.
    """


class ConnectionFailure(OperatorError):
    """The target PostgreSQL instance could not be reached or authenticated."""


class ReconcileFailure(OperatorError):
    """A reconcile step failed against PostgreSQL."""


class ObjectMissing(ReconcileFailure):
    """A statement referenced a database or role that no longer exists.

    Normal during teardown: a PostgresDB and the PostgresUsers that reference it
    are usually deleted together, so a user's cleanup can find its database
    already dropped. Steps that are cleaning up treat this as success.
    """


class InsufficientPrivilege(ReconcileFailure):
    """PostgreSQL refused a statement because the managing role may not run it.

    Distinct from its parent because it is a *permanent* fact about the server's
    grant state rather than a transient failure: retrying changes nothing. Steps
    that can degrade gracefully catch this specifically, so that an unrelated
    error is never mistaken for a permission problem.
    """


class SourceUnreachableFromTarget(ReconcileFailure):
    """The target *server* could not connect to the source.

    Distinct from :class:`ConnectionFailure`, which is the operator failing to
    reach a server. Here the operator reached both; what is broken is the path
    between them - security groups, routing, or the address in
    ``source.connection`` - which is a different fix entirely.
    """


class ReplicationRefused(ReconcileFailure):
    """The source refused the replication login a walsender connection.

    On RDS the login streams through rds_replication; on a stock server it needs
    the REPLICATION attribute. Retried, because both can be granted out of band.
    """


class InsufficientCapacity(ReconcileFailure):
    """The source has no free replication slot for a new subscription.

    Temporary: slots are freed by other subscriptions going away, or by raising
    max_replication_slots in the parameter group.
    """
