"""Typed models for the operator's custom resource specs.

The CRDs carry a full OpenAPI v3 schema so the API server rejects most bad
input, but the operator re-validates here: it is the only place that knows how
defaults are derived (role names, secret names, sanitised identifiers), and CRD
validation cannot express those relationships.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, model_validator

from .constants import ACCESS_OWNER, ACCESS_RO, ACCESS_RW, DROP, RETAIN
from .naming import (
    group_role_names,
    reject_reserved_role_prefix,
    sanitize_identifier,
    secret_name,
    validate_identifier,
    validate_role_name,
)
from .units import parse_duration, parse_quantity

RetentionPolicy = Literal["RETAIN", "DROP"]
AccessLevel = Literal["OWNER", "RW", "RO"]
SslMode = Literal["disable", "allow", "prefer", "require", "verify-ca", "verify-full"]

Identifier = Annotated[str, Field(min_length=1, max_length=63)]
K8sName = Annotated[str, Field(min_length=1, max_length=253)]


class Spec(BaseModel):
    """Base for spec models: reject unknown fields, accept camelCase as written."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)


# ---------------------------------------------------------------------------
# shared references
# ---------------------------------------------------------------------------


class SecretKeyRef(Spec):
    """Reference to a key inside a Secret, optionally in another namespace."""

    name: K8sName
    namespace: str | None = None
    key: str = "password"

    def resolve_namespace(self, default: str) -> str:
        return self.namespace or default


class CredentialsSecretRef(Spec):
    """Reference to the Secret holding an instance's superuser credentials."""

    name: K8sName
    namespace: str | None = None
    username_key: str = Field(default="username", alias="usernameKey")
    password_key: str = Field(default="password", alias="passwordKey")

    def resolve_namespace(self, default: str) -> str:
        return self.namespace or default


class ObjectRef(Spec):
    """Reference to another custom resource by name."""

    name: K8sName
    namespace: str | None = None

    def resolve_namespace(self, default: str) -> str:
        return self.namespace or default


class InstanceRef(Spec):
    """Reference to a cluster-scoped PostgresInstance."""

    name: K8sName


class SecretStoreRef(Spec):
    """external-secrets.io store reference."""

    name: K8sName
    kind: Literal["SecretStore", "ClusterSecretStore"] = "ClusterSecretStore"


class PushSecretSpec(Spec):
    """Opt-in configuration for mirroring credentials to an external store."""

    enabled: bool = True
    secret_store_refs: list[SecretStoreRef] = Field(
        default_factory=list, alias="secretStoreRefs"
    )
    remote_ref_key: str | None = Field(default=None, alias="remoteRefKey")
    #: Written to PushSecret.spec.updatePolicy.
    update_policy: Literal["Replace", "IfNotExists"] = Field(
        default="Replace", alias="updatePolicy"
    )
    #: Written to PushSecret.spec.deletionPolicy. ``None`` leaves the remote
    #: value in place when the PushSecret is removed.
    deletion_policy: Literal["Delete", "None"] = Field(
        default="None", alias="deletionPolicy"
    )
    refresh_interval: str | None = Field(default=None, alias="refreshInterval")
    #: Extra metadata merged onto the generated PushSecret.
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
    #: Restrict which Secret keys are pushed. Empty means all managed keys.
    keys: list[str] = Field(default_factory=list)
    #: Provider-specific push options (AWS Secrets Manager tags and
    #: description, Vault customMetadata, ...). Passed through verbatim as the
    #: ``spec`` of the PushSecretMetadata embedded in every data entry, because
    #: only the external-secrets provider knows what belongs here.
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _require_store_when_enabled(self) -> Self:
        if self.enabled and not self.secret_store_refs:
            raise ValueError(
                "pushSecret.secretStoreRefs must be set when pushSecret is enabled"
            )
        return self


# ---------------------------------------------------------------------------
# PostgresInstance
# ---------------------------------------------------------------------------


class InstanceSpec(Spec):
    """Spec of the cluster-scoped PostgresInstance resource."""

    host: Annotated[str, Field(min_length=1)]
    port: Annotated[int, Field(ge=1, le=65535)] = 5432
    credentials_secret_ref: CredentialsSecretRef = Field(alias="credentialsSecretRef")
    maintenance_database: Identifier = Field(default="postgres", alias="maintenanceDatabase")
    ssl_mode: SslMode = Field(default="require", alias="sslMode")
    ssl_root_cert_secret_ref: SecretKeyRef | None = Field(
        default=None, alias="sslRootCertSecretRef"
    )
    connect_timeout_seconds: Annotated[int, Field(ge=1, le=300)] = Field(
        default=10, alias="connectTimeoutSeconds"
    )
    #: Default retention policy inherited by PostgresDB/PostgresUser that omit it.
    retention_policy: RetentionPolicy = Field(default=RETAIN, alias="retentionPolicy")
    #: Prefix applied to every generated group role name on this instance.
    role_prefix: str = Field(default="", alias="rolePrefix")
    #: Instance-wide PushSecret default, overridable per PostgresUser.
    push_secret: PushSecretSpec | None = Field(default=None, alias="pushSecret")
    #: Namespaces permitted to reference this instance. Empty means all.
    allowed_namespaces: list[str] = Field(default_factory=list, alias="allowedNamespaces")
    #: Refuse to act if the server reports a version below this major number.
    min_server_version: int | None = Field(default=None, alias="minServerVersion")
    paused: bool = False

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if self.ssl_mode in {"verify-ca", "verify-full"} and not self.ssl_root_cert_secret_ref:
            raise ValueError(
                f"sslMode {self.ssl_mode!r} requires sslRootCertSecretRef"
            )
        if self.role_prefix:
            # The prefix is concatenated into role names, so it must be safe on
            # its own before it is ever interpolated.
            validate_identifier(self.role_prefix.rstrip("_") + "_x", kind="rolePrefix")
        return self

    def permits_namespace(self, namespace: str) -> bool:
        return not self.allowed_namespaces or namespace in self.allowed_namespaces


# ---------------------------------------------------------------------------
# PostgresDB
# ---------------------------------------------------------------------------


class SchemaSpec(Spec):
    """A schema to create inside the database."""

    name: Identifier
    #: Optional comment applied via COMMENT ON SCHEMA.
    comment: str | None = None

    @model_validator(mode="after")
    def _validate(self) -> Self:
        validate_identifier(self.name, kind="schema name")
        return self


class ExtensionSpec(Spec):
    """An extension to install inside the database."""

    name: Annotated[str, Field(min_length=1, max_length=63)]
    schema_: Identifier | None = Field(default=None, alias="schema")
    version: str | None = None
    #: Cascade to dependent extensions on CREATE EXTENSION.
    cascade: bool = False

    @model_validator(mode="after")
    def _validate(self) -> Self:
        validate_identifier(self.name, kind="extension name")
        if self.schema_:
            validate_identifier(self.schema_, kind="extension schema")
        return self


class RoleNameOverrides(Spec):
    """Explicit names for the generated group roles."""

    owner: Identifier | None = None
    read_write: Identifier | None = Field(default=None, alias="readWrite")
    read_only: Identifier | None = Field(default=None, alias="readOnly")

    @model_validator(mode="after")
    def _validate(self) -> Self:
        for field_name, value in (
            ("roles.owner", self.owner),
            ("roles.readWrite", self.read_write),
            ("roles.readOnly", self.read_only),
        ):
            if value:
                validate_role_name(value, kind=field_name)
        names = [v for v in (self.owner, self.read_write, self.read_only) if v]
        if len(names) != len(set(names)):
            raise ValueError("roles.owner/readWrite/readOnly must be distinct")
        return self


class DatabaseSpec(Spec):
    """Spec of the namespaced PostgresDB resource."""

    instance_ref: InstanceRef = Field(alias="instanceRef")
    #: Defaults to a sanitised form of metadata.name.
    database_name: Identifier | None = Field(default=None, alias="databaseName")
    retention_policy: RetentionPolicy | None = Field(default=None, alias="retentionPolicy")

    schemas: list[SchemaSpec] = Field(default_factory=list)
    extensions: list[ExtensionSpec] = Field(default_factory=list)
    roles: RoleNameOverrides = Field(default_factory=RoleNameOverrides)

    # Creation-time only; PostgreSQL cannot change these after CREATE DATABASE.
    encoding: str = "UTF8"
    lc_collate: str | None = Field(default=None, alias="lcCollate")
    lc_ctype: str | None = Field(default=None, alias="lcCtype")
    template: str = "template0"

    # Reconcilable database attributes.
    connection_limit: Annotated[int, Field(ge=-1)] = Field(
        default=-1, alias="connectionLimit"
    )
    #: ALTER DATABASE ... SET <key> = <value>
    parameters: dict[str, str] = Field(default_factory=dict)
    comment: str | None = None

    #: Revoke CONNECT/TEMP from PUBLIC so only the three group roles can attach.
    revoke_public_connect: bool = Field(default=True, alias="revokePublicConnect")
    #: Revoke CREATE (and, on PG < 15, USAGE stays) from PUBLIC on each schema.
    revoke_public_schema_create: bool = Field(
        default=True, alias="revokePublicSchemaCreate"
    )
    #: Grant EXECUTE on functions to the read-only role.
    grant_execute_to_read_only: bool = Field(
        default=True, alias="grantExecuteToReadOnly"
    )
    #: Make members of the owner group create objects as the group itself, by
    #: setting `role` per-database on each OWNER login role.
    set_role_for_owners: bool = Field(default=True, alias="setRoleForOwners")
    paused: bool = False

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if self.database_name:
            validate_identifier(self.database_name, kind="databaseName")
        names = [s.name for s in self.schemas]
        if len(names) != len(set(names)):
            raise ValueError("spec.schemas contains duplicate names")
        ext_names = [e.name for e in self.extensions]
        if len(ext_names) != len(set(ext_names)):
            raise ValueError("spec.extensions contains duplicate names")
        return self

    def resolve_database_name(self, resource_name: str) -> str:
        if self.database_name:
            return self.database_name
        return sanitize_identifier(resource_name, kind="databaseName")

    def resolve_schemas(self) -> list[SchemaSpec]:
        """Schemas to manage, always including ``public``.

        ``public`` exists in every database created from ``template0``/``template1``,
        so it is managed whether or not it is declared; leaving it out of the
        grant pass would silently deny the group roles access to it.
        """
        if any(s.name == "public" for s in self.schemas):
            return list(self.schemas)
        return [SchemaSpec(name="public"), *self.schemas]

    def resolve_role_names(self, database: str, prefix: str = "") -> dict[str, str]:
        defaults = group_role_names(database, prefix)
        resolved = {
            "owner": self.roles.owner or defaults["owner"],
            "readWrite": self.roles.read_write or defaults["readWrite"],
            "readOnly": self.roles.read_only or defaults["readOnly"],
        }
        if len(set(resolved.values())) != 3:
            raise ValueError(f"derived group role names collide: {resolved}")
        return resolved


# ---------------------------------------------------------------------------
# PostgresUser
# ---------------------------------------------------------------------------


class AccessSpec(Spec):
    """One grant: membership of a database's group role at a given level."""

    db_ref: ObjectRef = Field(alias="dbRef")
    role: AccessLevel

    @property
    def role_key(self) -> str:
        return {ACCESS_OWNER: "owner", ACCESS_RW: "readWrite", ACCESS_RO: "readOnly"}[
            self.role
        ]


class RoleAttributes(Spec):
    """Login-role attributes. Everything defaults off."""

    create_db: bool = Field(default=False, alias="createDB")
    create_role: bool = Field(default=False, alias="createRole")
    replication: bool = False
    bypass_rls: bool = Field(default=False, alias="bypassRLS")
    inherit: bool = True

    def as_mapping(self) -> dict[str, bool]:
        """Desired attributes, compared against the role's observed state."""
        return {
            "createDB": self.create_db,
            "createRole": self.create_role,
            "replication": self.replication,
            "bypassRLS": self.bypass_rls,
            "inherit": self.inherit,
        }


class GeneratedSecretSpec(Spec):
    """Shape of the Secret the operator writes for a user's credentials."""

    name: K8sName | None = None
    namespace: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
    #: Rename the keys written into the Secret.
    keys: dict[str, str] = Field(default_factory=dict)
    #: Emit a libpq/SQLAlchemy URI and a JDBC URL alongside the discrete fields.
    include_uri: bool = Field(default=True, alias="includeUri")
    #: Database used in the connection string; defaults to the first granted DB.
    default_database: str | None = Field(default=None, alias="defaultDatabase")


class UserSpec(Spec):
    """Spec of the namespaced PostgresUser resource."""

    instance_ref: InstanceRef = Field(alias="instanceRef")
    #: Defaults to a sanitised form of metadata.name.
    username: Identifier | None = None
    retention_policy: RetentionPolicy | None = Field(default=None, alias="retentionPolicy")

    access: list[AccessSpec] = Field(default_factory=list)
    attributes: RoleAttributes = Field(default_factory=RoleAttributes)
    connection_limit: Annotated[int, Field(ge=-1)] = Field(
        default=-1, alias="connectionLimit"
    )
    valid_until: str | None = Field(default=None, alias="validUntil")
    #: Per-role settings applied with ALTER ROLE ... SET.
    parameters: dict[str, str] = Field(default_factory=dict)

    #: Bring your own password instead of having one generated.
    password_secret_ref: SecretKeyRef | None = Field(
        default=None, alias="passwordSecretRef"
    )
    password_length: Annotated[int, Field(ge=16, le=128)] = Field(
        default=32, alias="passwordLength"
    )
    generated_secret: GeneratedSecretSpec = Field(
        default_factory=GeneratedSecretSpec, alias="generatedSecret"
    )
    push_secret: PushSecretSpec | None = Field(default=None, alias="pushSecret")
    #: Allow LOGIN. A false value creates a NOLOGIN role (useful for group-only roles).
    login: bool = True
    paused: bool = False

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if self.username:
            validate_role_name(self.username, kind="username")
        seen: set[tuple[str, str]] = set()
        for entry in self.access:
            key = (entry.db_ref.namespace or "", entry.db_ref.name)
            if key in seen:
                raise ValueError(
                    f"spec.access lists {entry.db_ref.name!r} more than once; "
                    "a user holds exactly one access level per database"
                )
            seen.add(key)
        return self

    def resolve_username(self, resource_name: str) -> str:
        if self.username:
            return self.username
        return reject_reserved_role_prefix(
            sanitize_identifier(resource_name, kind="username"), kind="username"
        )

    def resolve_secret_name(self, resource_name: str) -> str:
        return self.generated_secret.name or secret_name(resource_name)

    def resolve_secret_namespace(self, default: str) -> str:
        return self.generated_secret.namespace or default

    def secret_key(self, logical: str) -> str:
        return self.generated_secret.keys.get(logical, logical)


# ---------------------------------------------------------------------------
# PostgresReplication
# ---------------------------------------------------------------------------

PublishOperation = Literal["insert", "update", "delete", "truncate"]
FilterOperator = Literal["Eq", "Ne", "Lt", "Le", "Gt", "Ge", "In", "IsNull", "IsNotNull"]
SubscriptionStreaming = Literal["off", "on", "parallel"]

#: A row-filter operand. The CRD declares it int-or-string; strict types stop a
#: YAML boolean from being coerced to 1 on its way through the model.
FilterValue = StrictInt | StrictStr

ALL_PUBLISH_OPERATIONS: tuple[PublishOperation, ...] = ("insert", "update", "delete", "truncate")


class FilterPredicate(Spec):
    """One comparison in a row filter: a column, an operator and its operand."""

    column: Identifier
    op: FilterOperator
    value: FilterValue | None = None
    values: list[FilterValue] | None = None

    @model_validator(mode="after")
    def _validate(self) -> Self:
        validate_identifier(self.column, kind="rowFilter column")
        if self.op == "In":
            if not self.values or self.value is not None:
                raise ValueError(f"rowFilter {self.column} In takes values, not value")
        elif self.op in {"IsNull", "IsNotNull"}:
            if self.value is not None or self.values is not None:
                raise ValueError(f"rowFilter {self.column} {self.op} takes no operand")
        elif self.value is None or self.values is not None:
            raise ValueError(f"rowFilter {self.column} {self.op} takes value, not values")
        return self


class RowFilter(Spec):
    """Predicates joined by AND (``all``) or OR (``any``), one level deep.

    Structured rather than SQL text so that columns reach the server as
    identifiers and operands as literals - a filter is applied by the master
    user, and text cannot be proven not to escape its parentheses.
    """

    all_: list[FilterPredicate] | None = Field(default=None, alias="all")
    any_: list[FilterPredicate] | None = Field(default=None, alias="any")

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if bool(self.all_) == bool(self.any_):
            raise ValueError("rowFilter sets exactly one of all or any, with at least one entry")
        return self

    @property
    def predicates(self) -> list[FilterPredicate]:
        return self.all_ or self.any_ or []

    @property
    def conjunction(self) -> Literal["AND", "OR"]:
        return "AND" if self.all_ else "OR"

    @property
    def columns(self) -> frozenset[str]:
        """Columns the filter reads - which must lie inside the replica identity."""
        return frozenset(predicate.column for predicate in self.predicates)


class SchemaSelection(Spec):
    """Publish every eligible table in a schema, re-enumerated on each pass."""

    name: Identifier
    exclude_tables: list[Identifier] = Field(default_factory=list, alias="excludeTables")

    @model_validator(mode="after")
    def _validate(self) -> Self:
        validate_identifier(self.name, kind="schema name")
        for table in self.exclude_tables:
            validate_identifier(table, kind="excluded table")
        if len(self.exclude_tables) != len(set(self.exclude_tables)):
            raise ValueError(f"schemas[{self.name}].excludeTables contains duplicates")
        return self


class TableSelection(Spec):
    """Publish one table, optionally limited to some columns and rows."""

    schema_: Identifier = Field(alias="schema")
    name: Identifier
    #: Empty publishes every column.
    columns: list[Identifier] = Field(default_factory=list)
    row_filter: RowFilter | None = Field(default=None, alias="rowFilter")

    @model_validator(mode="after")
    def _validate(self) -> Self:
        validate_identifier(self.schema_, kind="table schema")
        validate_identifier(self.name, kind="table name")
        for column in self.columns:
            validate_identifier(column, kind="column")
        if len(self.columns) != len(set(self.columns)):
            raise ValueError(f"tables[{self.qualified}].columns contains duplicates")
        return self

    @property
    def qualified(self) -> str:
        return f"{self.schema_}.{self.name}"


class ReplicationConnection(Spec):
    """How the *target server* reaches the source - not how the operator does.

    Defaults to the source instance's endpoint. The two often differ: the
    operator may reach RDS through one name while another RDS instance in the
    VPC resolves it through a private one.
    """

    host: Annotated[str, Field(min_length=1)] | None = None
    port: Annotated[int, Field(ge=1, le=65535)] | None = None
    ssl_mode: SslMode = Field(default="require", alias="sslMode")
    #: A CA bundle path on the *target server's* filesystem, for verify-*.
    ssl_root_cert_path: Annotated[str, Field(min_length=1)] | None = Field(
        default=None, alias="sslRootCertPath"
    )

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if self.ssl_mode in {"verify-ca", "verify-full"} and not self.ssl_root_cert_path:
            raise ValueError(
                f"sslMode {self.ssl_mode!r} requires sslRootCertPath, a CA bundle "
                "path on the target server"
            )
        return self


class ReplicationSource(Spec):
    db_ref: ObjectRef = Field(alias="dbRef")
    connection: ReplicationConnection = Field(default_factory=ReplicationConnection)
    #: A pre-provisioned replication login, read and never written. Without it
    #: the operator generates one on the source.
    credentials_secret_ref: CredentialsSecretRef | None = Field(
        default=None, alias="credentialsSecretRef"
    )


class ReplicationTarget(Spec):
    db_ref: ObjectRef = Field(alias="dbRef")


class SubscriptionOptions(Spec):
    """Subscription settings passed through. Omitted means the server default."""

    streaming: SubscriptionStreaming | None = None
    binary: bool | None = None


class AlertingSpec(Spec):
    """Thresholds past which a replication reports itself unhealthy."""

    max_lag: StrictInt | StrictStr = Field(default="1Gi", alias="maxLag")
    max_retained_wal: StrictInt | StrictStr = Field(default="10Gi", alias="maxRetainedWal")
    max_slot_inactive: str = Field(default="10m", alias="maxSlotInactive")

    @model_validator(mode="after")
    def _validate(self) -> Self:
        for field_name, value in (
            ("maxLag", self.max_lag_bytes),
            ("maxRetainedWal", self.max_retained_wal_bytes),
            ("maxSlotInactive", self.max_slot_inactive_seconds),
        ):
            if value <= 0:
                raise ValueError(f"alerting.{field_name} must be greater than zero")
        return self

    @property
    def max_lag_bytes(self) -> int:
        return parse_quantity(self.max_lag)

    @property
    def max_retained_wal_bytes(self) -> int:
        return parse_quantity(self.max_retained_wal)

    @property
    def max_slot_inactive_seconds(self) -> int:
        return parse_duration(self.max_slot_inactive)


class ReplicationSpec(Spec):
    """Spec of the namespaced PostgresReplication resource."""

    source: ReplicationSource
    target: ReplicationTarget
    schemas: list[SchemaSelection] = Field(default_factory=list)
    tables: list[TableSelection] = Field(default_factory=list)
    publish: list[PublishOperation] = Field(
        default_factory=lambda: list(ALL_PUBLISH_OPERATIONS), min_length=1
    )
    publish_via_partition_root: bool = Field(default=False, alias="publishViaPartitionRoot")
    #: Copy existing rows on create, and for every table added later.
    copy_data: bool = Field(default=True, alias="copyData")
    #: False disables the subscription; the slot keeps retaining WAL meanwhile.
    enabled: bool = True
    subscription: SubscriptionOptions = Field(default_factory=SubscriptionOptions)
    alerting: AlertingSpec = Field(default_factory=AlertingSpec)
    paused: bool = False

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if not self.schemas and not self.tables:
            raise ValueError("a replication selects at least one entry in schemas or tables")
        schema_names = [entry.name for entry in self.schemas]
        if len(schema_names) != len(set(schema_names)):
            raise ValueError("spec.schemas lists a schema more than once")
        tables = [entry.qualified for entry in self.tables]
        if len(tables) != len(set(tables)):
            raise ValueError("spec.tables lists a table more than once")
        if len(self.publish) != len(set(self.publish)):
            raise ValueError("spec.publish lists an operation more than once")

        excluded = {
            f"{entry.name}.{table}" for entry in self.schemas for table in entry.exclude_tables
        }
        contradictions = sorted(excluded.intersection(tables))
        if contradictions:
            raise ValueError(
                f"{', '.join(contradictions)} is both excluded in spec.schemas and "
                "listed in spec.tables"
            )
        return self

    @property
    def selected_schemas(self) -> frozenset[str]:
        """Every schema the replication reads from, whole or in part."""
        return frozenset(
            [entry.name for entry in self.schemas] + [entry.schema_ for entry in self.tables]
        )

    @property
    def publishes_keyed_changes(self) -> bool:
        """UPDATE or DELETE is published, so each table needs a replica identity."""
        return "update" in self.publish or "delete" in self.publish


# ---------------------------------------------------------------------------
# parsing helpers
# ---------------------------------------------------------------------------


def parse_instance_spec(spec: dict[str, Any]) -> InstanceSpec:
    return InstanceSpec.model_validate(spec)


def parse_database_spec(spec: dict[str, Any]) -> DatabaseSpec:
    return DatabaseSpec.model_validate(spec)


def parse_user_spec(spec: dict[str, Any]) -> UserSpec:
    return UserSpec.model_validate(spec)


def parse_replication_spec(spec: dict[str, Any]) -> ReplicationSpec:
    return ReplicationSpec.model_validate(spec)


def effective_retention(
    child: RetentionPolicy | None, instance_default: RetentionPolicy
) -> RetentionPolicy:
    """Resolve a child resource's retention policy, defaulting to RETAIN."""
    if child in (RETAIN, DROP):
        return child
    return instance_default
