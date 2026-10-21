"""Response models for the state API.

These are deliberately a projection of the custom resources rather than a
pass-through of raw objects: the shape stays stable while the CRDs evolve, and
it is what the React frontend will bind to.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from ..status import DEGRADING_REASONS

Phase = Literal[
    "Ready", "Drifted", "Degraded", "Pending", "Paused", "Failed", "Unreachable", "Unknown"
]


class Condition(BaseModel):
    type: str
    status: str
    reason: str = ""
    message: str = ""
    last_transition_time: str | None = Field(default=None, alias="lastTransitionTime")
    observed_generation: int | None = Field(default=None, alias="observedGeneration")

    model_config = {"populate_by_name": True}


class ResourceRef(BaseModel):
    name: str
    namespace: str | None = None


class LogicalReplicationSummary(BaseModel):
    """Whether an instance could take part in a replication, as its status reports it."""

    wal_level: str | None = None
    max_replication_slots: int | None = None
    replication_slots_in_use: int | None = None
    max_wal_senders: int | None = None
    max_slot_wal_keep_size: str | None = None
    create_subscription: str | None = None
    replication_grant: str | None = None
    issues: list[str] = Field(default_factory=list)


class InstanceSummary(BaseModel):
    name: str
    phase: Phase = "Unknown"
    endpoint: str | None = None
    server_version: str | None = None
    server_version_text: str | None = None
    managing_role: str | None = None
    privileges: str | None = None
    ssl_mode: str | None = None
    retention_policy: str | None = None
    credentials_secret: str | None = None
    push_secrets_available: bool | None = None
    logical_replication: LogicalReplicationSummary | None = None
    databases: int = 0
    users: int = 0
    paused: bool = False
    last_connected_at: str | None = None
    observed_generation: int | None = None
    generation: int | None = None
    creation_timestamp: str | None = None
    conditions: list[Condition] = Field(default_factory=list)
    message: str | None = None


class GrantSummary(BaseModel):
    database: str
    role: str
    group_role: str
    db_ref: str


class GroupRoleSummary(BaseModel):
    owner: str | None = None
    read_write: str | None = None
    read_only: str | None = None


class DatabaseSummary(BaseModel):
    name: str
    namespace: str
    phase: Phase = "Unknown"
    instance: str | None = None
    endpoint: str | None = None
    database_name: str | None = None
    retention_policy: str | None = None
    roles: GroupRoleSummary = Field(default_factory=GroupRoleSummary)
    managed_schemas: list[str] = Field(default_factory=list)
    observed_schemas: list[str] = Field(default_factory=list)
    orphaned_schemas: list[str] = Field(default_factory=list)
    unmanaged_schemas: list[str] = Field(default_factory=list)
    unowned_schemas: list[str] = Field(default_factory=list)
    managed_extensions: list[str] = Field(default_factory=list)
    observed_extensions: list[str] = Field(default_factory=list)
    orphaned_extensions: list[str] = Field(default_factory=list)
    schema_grants: dict[str, list[str]] = Field(default_factory=dict)
    #: Parameters the managing role was refused, e.g. a superuser-only GUC.
    #: Reported rather than raised: the rest of the database still converges.
    denied_parameters: list[str] = Field(default_factory=list)
    paused: bool = False
    last_reconciled_at: str | None = None
    observed_generation: int | None = None
    generation: int | None = None
    creation_timestamp: str | None = None
    conditions: list[Condition] = Field(default_factory=list)
    message: str | None = None


class UserSummary(BaseModel):
    name: str
    namespace: str
    phase: Phase = "Unknown"
    instance: str | None = None
    endpoint: str | None = None
    username: str | None = None
    retention_policy: str | None = None
    grants: list[GrantSummary] = Field(default_factory=list)
    secret_ref: ResourceRef | None = None
    secret_keys: list[str] = Field(default_factory=list)
    push_secret_ref: ResourceRef | None = None
    login: bool = True
    paused: bool = False
    byo_password: bool = False
    last_reconciled_at: str | None = None
    observed_generation: int | None = None
    generation: int | None = None
    creation_timestamp: str | None = None
    conditions: list[Condition] = Field(default_factory=list)
    message: str | None = None


class ReplicationEndSummary(BaseModel):
    instance: str | None = None
    database: str | None = None
    #: ``namespace/name`` of the PostgresDB.
    postgres_db: str | None = None
    display: str | None = None


class ReplicationTables(BaseModel):
    published: int = 0
    subscribed: int = 0
    ready: int = 0
    syncing: int = 0
    excluded: int = 0


class ReplicationHealthSummary(BaseModel):
    """Measured on the replication's last pass. Absent values could not be read."""

    observed_at: str | None = None
    lag: str | None = None
    lag_bytes: int | None = None
    retained_wal: str | None = None
    retained_wal_bytes: int | None = None
    safe_wal_size_bytes: int | None = None
    slot_active: bool | None = None
    wal_status: str | None = None
    inactive_since: str | None = None
    worker_running: bool | None = None
    last_message_at: str | None = None
    apply_error_count: int | None = None
    sync_error_count: int | None = None
    conflicts: dict[str, int] = Field(default_factory=dict)


class ReplicationThresholds(BaseModel):
    """``spec.alerting``, resolved: what lag and retained WAL are measured against."""

    max_lag: str
    max_lag_bytes: int
    max_retained_wal: str
    max_retained_wal_bytes: int
    max_slot_inactive: str
    max_slot_inactive_seconds: int


class TableIssue(BaseModel):
    table: str
    reason: str
    message: str


class SubscribedTable(BaseModel):
    table: str
    #: ``i`` init, ``d`` copying, ``f``/``s`` syncing, ``r`` ready.
    state: str


class ReplicationSummary(BaseModel):
    name: str
    namespace: str
    phase: Phase = "Unknown"
    #: Initializing, Streaming, Lagging, Stalled, SlotLost or Disabled.
    state: str | None = None
    source: ReplicationEndSummary = Field(default_factory=ReplicationEndSummary)
    target: ReplicationEndSummary = Field(default_factory=ReplicationEndSummary)
    tables: ReplicationTables = Field(default_factory=ReplicationTables)
    health: ReplicationHealthSummary | None = None
    thresholds: ReplicationThresholds
    enabled: bool = True
    paused: bool = False
    publication: str | None = None
    subscription: str | None = None
    slot: str | None = None
    login: str | None = None
    #: Names the login's credentials Secret and its keys; never their values.
    credentials_secret: ResourceRef | None = None
    credentials_generated: bool | None = None
    secret_keys: list[str] = Field(default_factory=list)
    excluded_tables: list[TableIssue] = Field(default_factory=list)
    last_reconciled_at: str | None = None
    observed_generation: int | None = None
    generation: int | None = None
    creation_timestamp: str | None = None
    conditions: list[Condition] = Field(default_factory=list)
    message: str | None = None


class ReplicationDetail(ReplicationSummary):
    published_tables: list[str] = Field(default_factory=list)
    subscribed_tables: list[SubscribedTable] = Field(default_factory=list)
    table_warnings: list[TableIssue] = Field(default_factory=list)


class PhaseCounts(BaseModel):
    ready: int = 0
    drifted: int = 0
    degraded: int = 0
    failed: int = 0
    unreachable: int = 0
    pending: int = 0
    paused: int = 0
    unknown: int = 0
    total: int = 0


class Overview(BaseModel):
    """Aggregate health, for the dashboard landing view."""

    instances: PhaseCounts
    databases: PhaseCounts
    users: PhaseCounts
    replications: PhaseCounts = Field(default_factory=PhaseCounts)
    healthy: bool
    attention: list[str] = Field(default_factory=list)
    generated_at: str


class DatabaseDetail(DatabaseSummary):
    users: list[UserSummary] = Field(default_factory=list)
    #: Replications from or into this database; source and target say which.
    replications: list[ReplicationSummary] = Field(default_factory=list)


class InstanceDetail(InstanceSummary):
    database_list: list[DatabaseSummary] = Field(default_factory=list)
    user_list: list[UserSummary] = Field(default_factory=list)
    replication_list: list[ReplicationSummary] = Field(default_factory=list)


class Health(BaseModel):
    status: Literal["ok", "degraded"]
    kubernetes: bool
    crds: dict[str, bool] = Field(default_factory=dict)
    detail: str | None = None


class ErrorResponse(BaseModel):
    detail: str
    kind: str | None = None
    reference: str | None = None


def conditions_of(status: dict[str, Any]) -> list[Condition]:
    return [Condition.model_validate(entry) for entry in status.get("conditions") or []]


def phase_message(status: dict[str, Any]) -> str | None:
    """The one-liner that explains the resource's current phase.

    Ready's message is the right one when the resource is not ready, since it
    names the failure. But a Drifted resource *is* ready - it reconciled, and
    something about the live state diverges anyway - so quoting Ready there
    reports "in sync" next to a Drifted phase and tells the reader nothing.
    """
    conditions = {
        str(entry.get("type")): entry for entry in (status.get("conditions") or [])
    }
    ready = conditions.get("Ready")
    if ready is not None and ready.get("status") != "True":
        return str(ready.get("message") or "") or None

    # A Degraded replication reconciled; what is wrong is its data flow, which
    # its Streaming condition explains.
    streaming = conditions.get("Streaming")
    if streaming is not None and streaming.get("reason") in DEGRADING_REASONS:
        return str(streaming.get("message") or "") or None

    drifted = conditions.get("Drifted")
    if drifted is not None and drifted.get("status") == "True":
        return str(drifted.get("message") or "") or None

    return str((ready or {}).get("message") or "") or None
