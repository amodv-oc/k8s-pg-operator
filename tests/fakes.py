"""A faithful-enough fake of the Kubernetes API surface the operator uses.

Lets the real reconcilers run against a real PostgreSQL server without a
cluster, so the tests exercise ``reconcile_database`` and ``reconcile_user``
themselves rather than re-implementing their steps.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Any

from kubernetes_asyncio.client.exceptions import ApiException


def _not_found(what: str) -> ApiException:
    return ApiException(status=404, reason=f"{what} not found")


@dataclass
class FakeMetadata:
    name: str
    namespace: str | None = None
    labels: dict[str, str] = field(default_factory=dict)
    annotations: dict[str, str] = field(default_factory=dict)


@dataclass
class FakeSecret:
    metadata: FakeMetadata
    data: dict[str, str] = field(default_factory=dict)
    string_data: dict[str, str] | None = None
    # Kubernetes Secret .type field, not a credential.
    type: str = "Opaque"

    def decoded(self) -> dict[str, str]:
        return {k: base64.b64decode(v).decode() for k, v in self.data.items()}


class FakeCoreV1:
    """Namespaced Secret CRUD, matching the calls in k8s/secrets.py."""

    def __init__(self) -> None:
        self.secrets: dict[tuple[str, str], FakeSecret] = {}
        self.deleted: list[tuple[str, str]] = []

    def seed(self, namespace: str, name: str, data: dict[str, str]) -> None:
        self.secrets[(namespace, name)] = FakeSecret(
            metadata=FakeMetadata(name=name, namespace=namespace),
            data={k: base64.b64encode(v.encode()).decode() for k, v in data.items()},
        )

    def get(self, namespace: str, name: str) -> dict[str, str] | None:
        secret = self.secrets.get((namespace, name))
        return secret.decoded() if secret else None

    async def read_namespaced_secret(self, name: str, namespace: str) -> FakeSecret:
        secret = self.secrets.get((namespace, name))
        if secret is None:
            raise _not_found(f"Secret {namespace}/{name}")
        return secret

    async def create_namespaced_secret(self, namespace: str, body: Any) -> FakeSecret:
        key = (namespace, body.metadata.name)
        if key in self.secrets:
            raise ApiException(status=409, reason="already exists")
        self.secrets[key] = FakeSecret(
            metadata=FakeMetadata(
                name=body.metadata.name,
                namespace=namespace,
                labels=dict(body.metadata.labels or {}),
                annotations=dict(body.metadata.annotations or {}),
            ),
            data=dict(body.data or {}),
            type=body.type,
        )
        return self.secrets[key]

    async def patch_namespaced_secret(
        self, name: str, namespace: str, body: dict[str, Any], *, _content_type: str
    ) -> FakeSecret:
        _require_merge_patch(_content_type)
        secret = await self.read_namespaced_secret(name, namespace)
        metadata = body.get("metadata") or {}
        secret.metadata.labels.update(metadata.get("labels") or {})
        secret.metadata.annotations.update(metadata.get("annotations") or {})
        secret.data.update(body.get("data") or {})
        return secret

    async def delete_namespaced_secret(self, name: str, namespace: str) -> None:
        if (namespace, name) not in self.secrets:
            raise _not_found(f"Secret {namespace}/{name}")
        del self.secrets[(namespace, name)]
        self.deleted.append((namespace, name))


def _require_merge_patch(content_type: str) -> None:
    """Reject anything but a merge patch, as the API server does.

    kubernetes-asyncio infers the PATCH Content-Type from the body's Python
    type and picks ``application/json-patch+json`` for custom resources, which
    the API server answers with 400 "cannot unmarshal object into Go value of
    type []handlers.jsonPatchOp". The fakes enforce the same contract so the
    operator cannot regress to relying on that inference.
    """
    if content_type != "application/merge-patch+json":
        raise ApiException(
            status=400,
            reason=f"patch body sent as {content_type}, expected a merge patch",
        )


class FakeCustomObjects:
    """Cluster- and namespace-scoped custom resource access."""

    def __init__(self) -> None:
        #: (plural, namespace or "", name) -> object
        self.objects: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.deleted: list[tuple[str, str, str]] = []

    # -- seeding ---------------------------------------------------------
    def add(self, obj: dict[str, Any], plural: str) -> dict[str, Any]:
        meta = obj["metadata"]
        key = (plural, meta.get("namespace") or "", meta["name"])
        self.objects[key] = obj
        return obj

    def get_stored(self, plural: str, namespace: str, name: str) -> dict[str, Any] | None:
        return self.objects.get((plural, namespace, name))

    # -- reads -----------------------------------------------------------
    async def get_cluster_custom_object(
        self, group: str, version: str, plural: str, name: str
    ) -> dict[str, Any]:
        del group, version
        obj = self.objects.get((plural, "", name))
        if obj is None:
            raise _not_found(f"{plural}/{name}")
        return obj

    async def get_namespaced_custom_object(
        self, group: str, version: str, namespace: str, plural: str, name: str
    ) -> dict[str, Any]:
        del group, version
        obj = self.objects.get((plural, namespace, name))
        if obj is None:
            raise _not_found(f"{plural}/{namespace}/{name}")
        return obj

    async def list_cluster_custom_object(
        self, group: str, version: str, plural: str, **_: Any
    ) -> dict[str, Any]:
        del group, version
        return {"items": [obj for (p, _ns, _n), obj in self.objects.items() if p == plural]}

    async def list_namespaced_custom_object(
        self, group: str, version: str, namespace: str, plural: str, **_: Any
    ) -> dict[str, Any]:
        del group, version
        return {
            "items": [
                obj for (p, ns, _n), obj in self.objects.items() if p == plural and ns == namespace
            ]
        }

    # -- writes ----------------------------------------------------------
    async def create_namespaced_custom_object(
        self, group: str, version: str, namespace: str, plural: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        del group, version
        key = (plural, namespace, body["metadata"]["name"])
        if key in self.objects:
            raise ApiException(status=409, reason="already exists")
        self.objects[key] = body
        return body

    async def patch_namespaced_custom_object(
        self,
        group: str,
        version: str,
        namespace: str,
        plural: str,
        name: str,
        body: dict[str, Any],
        _content_type: str,
    ) -> dict[str, Any]:
        _require_merge_patch(_content_type)
        del group, version
        obj = await self.get_namespaced_custom_object(
            group="", version="", namespace=namespace, plural=plural, name=name
        )
        for section, value in body.items():
            if isinstance(value, dict) and isinstance(obj.get(section), dict):
                obj[section].update(value)
            else:
                obj[section] = value
        return obj

    async def delete_namespaced_custom_object(
        self, group: str, version: str, namespace: str, plural: str, name: str
    ) -> None:
        del group, version
        key = (plural, namespace, name)
        if key not in self.objects:
            raise _not_found(f"{plural}/{namespace}/{name}")
        del self.objects[key]
        self.deleted.append(key)

    async def patch_namespaced_custom_object_status(
        self,
        group: str,
        version: str,
        namespace: str,
        plural: str,
        name: str,
        body: dict[str, Any],
        _content_type: str,
    ) -> dict[str, Any]:
        _require_merge_patch(_content_type)
        obj = await self.get_namespaced_custom_object(
            group=group, version=version, namespace=namespace, plural=plural, name=name
        )
        obj["status"] = body["status"]
        return obj

    async def patch_cluster_custom_object_status(
        self,
        group: str,
        version: str,
        plural: str,
        name: str,
        body: dict[str, Any],
        _content_type: str,
    ) -> dict[str, Any]:
        _require_merge_patch(_content_type)
        obj = await self.get_cluster_custom_object(
            group=group, version=version, plural=plural, name=name
        )
        obj["status"] = body["status"]
        return obj


@dataclass
class FakeCrdVersion:
    name: str
    served: bool = True


@dataclass
class FakeCrdSpec:
    versions: list[FakeCrdVersion]


@dataclass
class FakeCrd:
    spec: FakeCrdSpec


class FakeApiExtensions:
    """CRD reads, used to detect whether external-secrets is installed."""

    def __init__(self, crds: dict[str, list[str]] | None = None) -> None:
        self.crds = crds or {}

    async def read_custom_resource_definition(self, name: str) -> FakeCrd:
        versions = self.crds.get(name)
        if versions is None:
            raise _not_found(f"CustomResourceDefinition {name}")
        return FakeCrd(spec=FakeCrdSpec([FakeCrdVersion(v) for v in versions]))


class FakeK8sClient:
    """Duck-typed stand-in for :class:`pg_operator.k8s.client.K8sClient`."""

    def __init__(self, crds: dict[str, list[str]] | None = None) -> None:
        self.core = FakeCoreV1()
        self.custom = FakeCustomObjects()
        self.apiextensions = FakeApiExtensions(crds)
        self.api = None

    async def close(self) -> None:
        return None
