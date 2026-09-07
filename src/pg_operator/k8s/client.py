"""Shared Kubernetes API client.

One long-lived ``ApiClient`` is used by both the operator and the API process;
kubernetes-asyncio holds a connection pool on it, so creating one per call would
leak sockets.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from kubernetes_asyncio import client, config
from kubernetes_asyncio.client.exceptions import ApiException

log = logging.getLogger(__name__)

# kubernetes-asyncio derives the PATCH Content-Type from the body's Python type.
# For custom resources the generated client offers only json-patch and
# merge-patch, and its heuristic returns the *first* entry, so a dict body is
# sent as ``application/json-patch+json`` and the API server rejects it with
# "cannot unmarshal object into Go value of type []handlers.jsonPatchOp".
# Every PATCH in this operator is a merge patch, so state it explicitly rather
# than relying on that heuristic.
MERGE_PATCH = "application/merge-patch+json"


async def merge_patch(fn: Callable[..., Awaitable[Any]], /, **kwargs: Any) -> Any:
    """Invoke a kubernetes-asyncio patch method as an explicit merge patch.

    ``_content_type`` is accepted by the generated client at runtime but is
    missing from its type stubs, so the method is taken as a plain callable.
    """
    return await fn(_content_type=MERGE_PATCH, **kwargs)


@dataclass(slots=True)
class K8sClient:
    """Typed façade over the API groups the operator touches."""

    api: client.ApiClient
    core: client.CoreV1Api
    custom: client.CustomObjectsApi
    apiextensions: client.ApiextensionsV1Api

    @classmethod
    async def create(cls) -> K8sClient:
        try:
            config.load_incluster_config()
            log.info("loaded in-cluster Kubernetes configuration")
        except config.ConfigException:
            await config.load_kube_config()
            log.info("loaded local kubeconfig")
        api = client.ApiClient()
        return cls(
            api=api,
            core=client.CoreV1Api(api),
            custom=client.CustomObjectsApi(api),
            apiextensions=client.ApiextensionsV1Api(api),
        )

    async def close(self) -> None:
        await self.api.close()


def is_not_found(exc: BaseException) -> bool:
    return isinstance(exc, ApiException) and exc.status == 404


def is_conflict(exc: BaseException) -> bool:
    return isinstance(exc, ApiException) and exc.status == 409


def is_forbidden(exc: BaseException) -> bool:
    return isinstance(exc, ApiException) and exc.status == 403
