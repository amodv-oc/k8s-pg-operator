"""external-secrets.io PushSecret management.

The operator does not talk to AWS Secrets Manager (or any other backend)
itself — it writes a Kubernetes Secret and then declares a PushSecret so
external-secrets mirrors it outward. That keeps backend credentials and
provider logic entirely inside external-secrets.

The API version is configurable because the resource moved from
``external-secrets.io/v1alpha1`` to ``external-secrets.io/v1`` in
external-secrets 0.14; the emitted body is compatible with both.
"""

from __future__ import annotations

import logging
import re
from copy import deepcopy
from typing import Any

from ..config import Settings
from ..constants import (
    PUSHSECRET_KIND,
    PUSHSECRET_METADATA_API_VERSION,
    PUSHSECRET_METADATA_KIND,
    PUSHSECRET_PLURAL,
)
from ..models import PushSecretSpec, SecretStoreRef
from .client import K8sClient, is_not_found, merge_patch

log = logging.getLogger(__name__)


def split_api_version(api_version: str) -> tuple[str, str]:
    group, _, version = api_version.partition("/")
    if not version:
        raise ValueError(f"pushSecret apiVersion {api_version!r} must be group/version")
    return group, version


def render_remote_key(template: str, *, namespace: str, name: str, username: str) -> str:
    """Expand the small placeholder set allowed in ``remoteRefKey``.

    Deliberately not a full template language: the value becomes a path in an
    external secret store, so the substitutions are fixed and predictable.
    """
    return (
        template.replace("{{ .namespace }}", namespace)
        .replace("{{ .name }}", name)
        .replace("{{ .username }}", username)
        .replace("{{namespace}}", namespace)
        .replace("{{name}}", name)
        .replace("{{username}}", username)
    )


def build_push_secret_metadata(spec: PushSecretSpec) -> dict[str, Any] | None:
    """Wrap ``pushSecret.metadata`` in a PushSecretMetadata envelope.

    external-secrets reads this document with the *provider's* decoder, so the
    body is passed through exactly as written — validating it here would mean
    encoding one provider's schema into a provider-agnostic operator. Returns
    ``None`` when nothing was configured, so the emitted body keeps its
    previous shape and steady state stays a no-op.
    """
    if not spec.metadata:
        return None
    return {
        "apiVersion": PUSHSECRET_METADATA_API_VERSION,
        "kind": PUSHSECRET_METADATA_KIND,
        "spec": deepcopy(spec.metadata),
    }


def key_match_regexp(keys: list[str]) -> str:
    """Anchored pattern matching exactly ``keys`` and nothing else.

    external-secrets tests the pattern with Go's ``regexp.MatchString``, which
    is unanchored, so ``^(?:...)$`` is required — bare ``port`` would also
    select a key named ``export_port``.
    """
    return "^(?:" + "|".join(re.escape(key) for key in keys) + ")$"


def _data_to_entry(
    ref: SecretStoreRef,
    *,
    remote_key: str,
    keys: list[str],
    metadata: dict[str, Any] | None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "storeRef": {"name": ref.name, "kind": ref.kind},
        # A `remoteKey` on dataTo bundles every matched key into one remote
        # object written by a single PutSecretValue. The alternative — one
        # `data` entry per key, each with its own `remoteRef.property` — makes
        # every key an independent read-modify-write of the *same* remote
        # object, with no compare-and-swap. Against an eventually consistent
        # store like AWS Secrets Manager those pushes silently lose each
        # other's keys. See README's "One object, one write".
        "remoteKey": remote_key,
        # Defaulted by the CRD; emitted so a converged spec compares equal and
        # the steady-state reconcile stays a genuine no-op.
        "conversionStrategy": "None",
    }
    if keys:
        entry["match"] = {"regexp": key_match_regexp(keys)}
    if metadata is not None:
        entry["metadata"] = deepcopy(metadata)
    return entry


def build_push_secret(
    spec: PushSecretSpec,
    *,
    api_version: str,
    name: str,
    namespace: str,
    source_secret: str,
    secret_keys: list[str],
    remote_key: str,
    labels: dict[str, str],
) -> dict[str, Any]:
    """Build the PushSecret body that mirrors one credentials Secret."""
    keys = spec.keys or secret_keys
    metadata = build_push_secret_metadata(spec)
    body: dict[str, Any] = {
        "apiVersion": api_version,
        "kind": PUSHSECRET_KIND,
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {**labels, **spec.labels},
            "annotations": dict(spec.annotations),
        },
        "spec": {
            "updatePolicy": spec.update_policy,
            "deletionPolicy": spec.deletion_policy,
            "secretStoreRefs": [
                {"name": ref.name, "kind": ref.kind} for ref in spec.secret_store_refs
            ],
            "selector": {"secret": {"name": source_secret}},
            "dataTo": [
                _data_to_entry(
                    ref, remote_key=remote_key, keys=keys, metadata=metadata
                )
                for ref in spec.secret_store_refs
            ],
        },
    }
    if spec.refresh_interval:
        body["spec"]["refreshInterval"] = spec.refresh_interval
    return body


async def apply_push_secret(
    k8s: K8sClient, settings: Settings, body: dict[str, Any]
) -> tuple[bool, bool]:
    """Create or update a PushSecret. Returns ``(created, changed)``."""
    group, version = split_api_version(str(body["apiVersion"]))
    namespace = body["metadata"]["namespace"]
    name = body["metadata"]["name"]
    del settings

    try:
        existing = await k8s.custom.get_namespaced_custom_object(
            group=group,
            version=version,
            namespace=namespace,
            plural=PUSHSECRET_PLURAL,
            name=name,
        )
    except Exception as exc:
        if not is_not_found(exc):
            raise
        await k8s.custom.create_namespaced_custom_object(
            group=group,
            version=version,
            namespace=namespace,
            plural=PUSHSECRET_PLURAL,
            body=body,
        )
        log.info("created PushSecret %s/%s", namespace, name)
        return True, True

    if existing.get("spec") == body["spec"] and _metadata_matches(existing, body):
        return False, False

    spec = dict(body["spec"])
    if (existing.get("spec") or {}).get("data") is not None:
        # Upgrade path off the per-property layout. A JSON merge patch only
        # deletes a field when it is explicitly null, so without this the old
        # `data` entries would survive alongside the new `dataTo` and
        # external-secrets would push both.
        spec["data"] = None

    await merge_patch(
        k8s.custom.patch_namespaced_custom_object,
        group=group,
        version=version,
        namespace=namespace,
        plural=PUSHSECRET_PLURAL,
        name=name,
        body={"metadata": body["metadata"], "spec": spec},
    )
    log.info("updated PushSecret %s/%s", namespace, name)
    return False, True


async def delete_push_secret(
    k8s: K8sClient, api_version: str, namespace: str, name: str
) -> bool:
    """Delete a PushSecret. Whether the remote value survives is decided by its
    own ``deletionPolicy``, which defaults to ``None`` (leave it in place)."""
    group, version = split_api_version(api_version)
    try:
        await k8s.custom.delete_namespaced_custom_object(
            group=group,
            version=version,
            namespace=namespace,
            plural=PUSHSECRET_PLURAL,
            name=name,
        )
    except Exception as exc:
        if is_not_found(exc):
            return False
        raise
    log.info("deleted PushSecret %s/%s", namespace, name)
    return True


async def push_secret_status(
    k8s: K8sClient, api_version: str, namespace: str, name: str
) -> dict[str, Any] | None:
    """Read a PushSecret's status so the operator can surface sync failures."""
    group, version = split_api_version(api_version)
    try:
        obj = await k8s.custom.get_namespaced_custom_object(
            group=group,
            version=version,
            namespace=namespace,
            plural=PUSHSECRET_PLURAL,
            name=name,
        )
    except Exception as exc:
        if is_not_found(exc):
            return None
        raise
    return obj.get("status") or {}


async def crd_available(k8s: K8sClient, api_version: str) -> bool:
    """Whether the PushSecret CRD is installed *and* serves ``api_version``.

    Checked so a missing or version-mismatched external-secrets installation
    reports as one clear condition on the instance, rather than as a 404 on
    every user reconcile.
    """
    group, version = split_api_version(api_version)
    name = f"{PUSHSECRET_PLURAL}.{group}"
    try:
        crd = await k8s.apiextensions.read_custom_resource_definition(name=name)
    except Exception as exc:
        if is_not_found(exc):
            return False
        raise
    return any(
        entry.name == version and entry.served for entry in (crd.spec.versions or [])
    )


def _metadata_matches(existing: dict[str, Any], desired: dict[str, Any]) -> bool:
    current = existing.get("metadata") or {}
    wanted = desired.get("metadata") or {}
    for field in ("labels", "annotations"):
        for key, value in (wanted.get(field) or {}).items():
            if (current.get(field) or {}).get(key) != value:
                return False
    return True
