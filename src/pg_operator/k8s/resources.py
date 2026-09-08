"""Reads and status writes for the operator's own custom resources."""

from __future__ import annotations

import logging
from typing import Any

from ..constants import (
    API_GROUP,
    API_VERSION,
    PLURAL_DATABASE,
    PLURAL_INSTANCE,
    PLURAL_USER,
)
from ..errors import InstanceNotFound, ReferenceNotFound
from .client import K8sClient, is_not_found, merge_patch

log = logging.getLogger(__name__)


async def get_instance(k8s: K8sClient, name: str) -> dict[str, Any]:
    """Fetch a cluster-scoped PostgresInstance by name."""
    try:
        return await k8s.custom.get_cluster_custom_object(
            group=API_GROUP, version=API_VERSION, plural=PLURAL_INSTANCE, name=name
        )
    except Exception as exc:
        if is_not_found(exc):
            raise InstanceNotFound(
                f"PostgresInstance {name!r} not found (it is cluster-scoped, "
                "so no namespace applies)"
            ) from exc
        raise


async def get_database(k8s: K8sClient, namespace: str, name: str) -> dict[str, Any]:
    try:
        return await k8s.custom.get_namespaced_custom_object(
            group=API_GROUP,
            version=API_VERSION,
            namespace=namespace,
            plural=PLURAL_DATABASE,
            name=name,
        )
    except Exception as exc:
        if is_not_found(exc):
            raise ReferenceNotFound(
                f"PostgresDB {namespace}/{name} not found"
            ) from exc
        raise


async def list_instances(k8s: K8sClient) -> list[dict[str, Any]]:
    result = await k8s.custom.list_cluster_custom_object(
        group=API_GROUP, version=API_VERSION, plural=PLURAL_INSTANCE
    )
    return list(result.get("items") or [])


async def list_databases(
    k8s: K8sClient, namespace: str | None = None
) -> list[dict[str, Any]]:
    return await _list_namespaced(k8s, PLURAL_DATABASE, namespace)


async def list_users(k8s: K8sClient, namespace: str | None = None) -> list[dict[str, Any]]:
    return await _list_namespaced(k8s, PLURAL_USER, namespace)


async def _list_namespaced(
    k8s: K8sClient, plural: str, namespace: str | None
) -> list[dict[str, Any]]:
    if namespace:
        result = await k8s.custom.list_namespaced_custom_object(
            group=API_GROUP, version=API_VERSION, namespace=namespace, plural=plural
        )
    else:
        result = await k8s.custom.list_cluster_custom_object(
            group=API_GROUP, version=API_VERSION, plural=plural
        )
    return list(result.get("items") or [])


async def patch_status(
    k8s: K8sClient,
    plural: str,
    name: str,
    status: dict[str, Any],
    namespace: str | None = None,
) -> None:
    """Patch a resource's status subresource, tolerating a concurrent delete."""
    body = {"status": status}
    try:
        if namespace:
            await merge_patch(
                k8s.custom.patch_namespaced_custom_object_status,
                group=API_GROUP,
                version=API_VERSION,
                namespace=namespace,
                plural=plural,
                name=name,
                body=body,
            )
        else:
            await merge_patch(
                k8s.custom.patch_cluster_custom_object_status,
                group=API_GROUP,
                version=API_VERSION,
                plural=plural,
                name=name,
                body=body,
            )
    except Exception as exc:
        if is_not_found(exc):
            log.debug("skipping status patch for deleted %s %s", plural, name)
            return
        raise
