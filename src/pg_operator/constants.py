"""Group, version and metadata constants shared across the operator."""

from __future__ import annotations

from typing import Final

API_GROUP: Final = "postgres.ourcommunity.com.au"
API_VERSION: Final = "v1alpha1"

KIND_INSTANCE: Final = "PostgresInstance"
KIND_DATABASE: Final = "PostgresDB"
KIND_USER: Final = "PostgresUser"

PLURAL_INSTANCE: Final = "postgresinstances"
PLURAL_DATABASE: Final = "postgresdbs"
PLURAL_USER: Final = "postgresusers"

FINALIZER: Final = f"{API_GROUP}/finalizer"

# Labels stamped on every object the operator creates (Secrets, PushSecrets).
LABEL_MANAGED_BY: Final = "app.kubernetes.io/managed-by"
LABEL_MANAGED_BY_VALUE: Final = "pg-operator"
LABEL_OWNER_KIND: Final = f"{API_GROUP}/owner-kind"
LABEL_OWNER_NAME: Final = f"{API_GROUP}/owner-name"
LABEL_OWNER_NAMESPACE: Final = f"{API_GROUP}/owner-namespace"
LABEL_INSTANCE: Final = f"{API_GROUP}/instance"

# Retention policies.
RETAIN: Final = "RETAIN"
DROP: Final = "DROP"

# Access levels.
ACCESS_OWNER: Final = "OWNER"
ACCESS_RW: Final = "RW"
ACCESS_RO: Final = "RO"
ACCESS_LEVELS: Final = (ACCESS_OWNER, ACCESS_RW, ACCESS_RO)

# Condition types published on .status.conditions.
COND_READY: Final = "Ready"
COND_REACHABLE: Final = "Reachable"
COND_SYNCED: Final = "Synced"
COND_DRIFTED: Final = "Drifted"

# external-secrets.io PushSecret coordinates. Upstream still serves PushSecret
# as v1alpha1 — the 0.14 promotion to v1 covered ExternalSecret/SecretStore but
# not this kind — so the version stays overridable via the chart.
PUSHSECRET_KIND: Final = "PushSecret"
PUSHSECRET_PLURAL: Final = "pushsecrets"

# Provider-specific push options travel inside each data entry as an embedded
# PushSecretMetadata document. The shape of its `spec` is decided by the
# provider (AWS Secrets Manager, Vault, GCP, ...), so the operator only supplies
# the envelope and passes the body through untouched.
PUSHSECRET_METADATA_API_VERSION: Final = "kubernetes.external-secrets.io/v1alpha1"
PUSHSECRET_METADATA_KIND: Final = "PushSecretMetadata"
