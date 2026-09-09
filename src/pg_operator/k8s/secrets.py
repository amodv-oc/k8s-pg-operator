"""Cross-namespace Secret reads and writes.

The operator reads instance superuser credentials from wherever they live and
writes generated user credentials into the consuming team's namespace, so its
RBAC covers Secrets cluster-wide. Reads are narrowed to a named object rather
than listing, and values are never logged.
"""

from __future__ import annotations

import base64
import logging
import secrets
import string
from urllib.parse import quote

from kubernetes_asyncio import client

from ..constants import (
    LABEL_INSTANCE,
    LABEL_MANAGED_BY,
    LABEL_MANAGED_BY_VALUE,
    LABEL_OWNER_KIND,
    LABEL_OWNER_NAME,
    LABEL_OWNER_NAMESPACE,
)
from ..errors import ReferenceNotFound
from .client import K8sClient, is_not_found, merge_patch

log = logging.getLogger(__name__)

#: Password alphabet: URL- and shell-safe, and free of characters that need
#: escaping inside a libpq connection string or a JDBC URL.
_PASSWORD_ALPHABET = string.ascii_letters + string.digits + "-_.~"


def generate_password(length: int = 32) -> str:
    """A cryptographically random password of ``length`` characters."""
    if length < 16:
        raise ValueError("password length must be at least 16 characters")
    return "".join(secrets.choice(_PASSWORD_ALPHABET) for _ in range(length))


async def read_secret(
    k8s: K8sClient, namespace: str, name: str
) -> dict[str, bytes]:
    """Read and base64-decode every key of a Secret."""
    try:
        secret = await k8s.core.read_namespaced_secret(name=name, namespace=namespace)
    except Exception as exc:
        if is_not_found(exc):
            raise ReferenceNotFound(
                f"Secret {namespace}/{name} not found"
            ) from exc
        raise
    data: dict[str, bytes] = {}
    for key, value in (secret.data or {}).items():
        data[key] = base64.b64decode(value)
    for key, value in (secret.string_data or {}).items():
        data[key] = value.encode()
    return data


async def read_secret_key(
    k8s: K8sClient, namespace: str, name: str, key: str
) -> bytes:
    """Read one key, with an error that names the missing key rather than the Secret."""
    data = await read_secret(k8s, namespace, name)
    if key not in data:
        available = ", ".join(sorted(data)) or "<none>"
        raise ReferenceNotFound(
            f"Secret {namespace}/{name} has no key {key!r} (keys present: {available})"
        )
    return data[key]


def managed_labels(
    *,
    owner_kind: str,
    owner_name: str,
    owner_namespace: str,
    instance: str | None = None,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """Labels stamped on every object the operator creates.

    Owner references cannot cross namespaces, so these labels are what makes a
    generated Secret traceable — and garbage-collectable — when it lives outside
    its resource's namespace.
    """
    labels = {
        LABEL_MANAGED_BY: LABEL_MANAGED_BY_VALUE,
        LABEL_OWNER_KIND: owner_kind,
        LABEL_OWNER_NAME: owner_name,
        LABEL_OWNER_NAMESPACE: owner_namespace,
    }
    if instance:
        labels[LABEL_INSTANCE] = instance
    labels.update(extra or {})
    return labels


async def apply_secret(
    k8s: K8sClient,
    *,
    namespace: str,
    name: str,
    data: dict[str, str],
    labels: dict[str, str],
    annotations: dict[str, str] | None = None,
    secret_type: str = "Opaque",  # noqa: S107 - Kubernetes Secret .type field
) -> tuple[bool, bool]:
    """Create or update a Secret. Returns ``(created, changed)``.

    An existing Secret is patched rather than replaced so that keys written by
    other controllers survive, and the patch is skipped entirely when the
    managed keys already match — which keeps a steady-state reconcile from
    bumping ``resourceVersion`` and re-triggering watchers.

    No owner reference is set. Owner references cannot cross namespaces, and a
    RETAIN policy has to leave a working credential behind for a role that still
    exists, so deletion is driven explicitly by the retention policy instead of
    by garbage collection.
    """
    encoded = {key: base64.b64encode(value.encode()).decode() for key, value in data.items()}
    metadata = client.V1ObjectMeta(
        name=name,
        namespace=namespace,
        labels=labels,
        annotations=annotations or {},
    )

    try:
        existing = await k8s.core.read_namespaced_secret(name=name, namespace=namespace)
    except Exception as exc:
        if not is_not_found(exc):
            raise
        await k8s.core.create_namespaced_secret(
            namespace=namespace,
            body=client.V1Secret(metadata=metadata, type=secret_type, data=encoded),
        )
        log.info("created Secret %s/%s", namespace, name)
        return True, True

    current = existing.data or {}
    data_matches = all(current.get(key) == value for key, value in encoded.items())
    labels_match = all(
        (existing.metadata.labels or {}).get(key) == value for key, value in labels.items()
    )
    annotations_match = all(
        (existing.metadata.annotations or {}).get(key) == value
        for key, value in (annotations or {}).items()
    )
    if data_matches and labels_match and annotations_match:
        return False, False

    await merge_patch(
        k8s.core.patch_namespaced_secret,
        name=name,
        namespace=namespace,
        body={
            "metadata": {"labels": labels, "annotations": annotations or {}},
            "data": encoded,
        },
    )
    log.info("updated Secret %s/%s", namespace, name)
    return False, True


async def delete_secret(k8s: K8sClient, namespace: str, name: str) -> bool:
    """Delete a Secret, tolerating a prior deletion. True if it was removed."""
    try:
        await k8s.core.delete_namespaced_secret(name=name, namespace=namespace)
    except Exception as exc:
        if is_not_found(exc):
            return False
        raise
    log.info("deleted Secret %s/%s", namespace, name)
    return True


def connection_keys(
    *,
    username: str,
    password: str,
    host: str,
    port: int,
    database: str,
    ssl_mode: str,
    include_uri: bool,
) -> dict[str, str]:
    """The logical key/value pairs written into a generated credentials Secret."""
    data = {
        "username": username,
        "password": password,
        "host": host,
        "port": str(port),
        "database": database,
        "sslmode": ssl_mode,
    }
    if include_uri:
        safe_user = quote(username, safe="")
        safe_password = quote(password, safe="")
        data["uri"] = (
            f"postgresql://{safe_user}:{safe_password}@{host}:{port}/{database}"
            f"?sslmode={ssl_mode}"
        )
        data["jdbcUri"] = (
            f"jdbc:postgresql://{host}:{port}/{database}"
            f"?user={safe_user}&password={safe_password}&sslmode={ssl_mode}"
        )
        data["springDatasourceUri"] = (
            f"jdbc:postgresql://{host}:{port}/{database}"
        )
    return data
