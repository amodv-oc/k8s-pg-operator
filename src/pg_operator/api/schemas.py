"""Response models for the state API.

These are deliberately a projection of the custom resources rather than a
pass-through of raw objects: the shape stays stable while the CRDs evolve, and
it is what the React frontend will bind to.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

Phase = Literal["Ready", "Drifted", "Pending", "Paused", "Failed", "Unreachable", "Unknown"]


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


class PhaseCounts(BaseModel):
    ready: int = 0
    drifted: int = 0
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
    healthy: bool
    attention: list[str] = Field(default_factory=list)
    generated_at: str


class DatabaseDetail(DatabaseSummary):
    users: list[UserSummary] = Field(default_factory=list)


class InstanceDetail(InstanceSummary):
    database_list: list[DatabaseSummary] = Field(default_factory=list)
    user_list: list[UserSummary] = Field(default_factory=list)


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

    drifted = conditions.get("Drifted")
    if drifted is not None and drifted.get("status") == "True":
        return str(drifted.get("message") or "") or None

    return str((ready or {}).get("message") or "") or None
