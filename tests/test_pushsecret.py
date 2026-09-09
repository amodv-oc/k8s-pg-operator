"""PushSecret body construction and remote-key templating."""

from __future__ import annotations

import re

import pytest

from pg_operator.config import Settings
from pg_operator.k8s.pushsecret import (
    apply_push_secret,
    build_push_secret,
    render_remote_key,
    split_api_version,
)
from pg_operator.models import PushSecretSpec

from .fakes import FakeK8sClient

SPEC = PushSecretSpec.model_validate(
    {
        "secretStoreRefs": [{"name": "aws-sm", "kind": "ClusterSecretStore"}],
        "remoteRefKey": "prod/{{ .namespace }}/{{ .name }}",
    }
)


def _build(spec: PushSecretSpec = SPEC, api_version: str = "external-secrets.io/v1"):
    return build_push_secret(
        spec,
        api_version=api_version,
        name="orders-api-pg-push",
        namespace="team-a",
        source_secret="orders-api-pg-credentials",
        secret_keys=["username", "password", "uri"],
        remote_key="prod/team-a/orders-api",
        labels={"app.kubernetes.io/managed-by": "pg-operator"},
    )


def test_api_version_is_split_into_group_and_version() -> None:
    assert split_api_version("external-secrets.io/v1") == ("external-secrets.io", "v1")
    assert split_api_version("external-secrets.io/v1alpha1") == (
        "external-secrets.io",
        "v1alpha1",
    )


def test_api_version_without_a_group_is_rejected() -> None:
    with pytest.raises(ValueError, match="group/version"):
        split_api_version("v1")


def test_body_targets_the_configured_api_version() -> None:
    assert _build()["apiVersion"] == "external-secrets.io/v1"
    # Switching to the older API is a values change, not a code change.
    assert _build(api_version="external-secrets.io/v1alpha1")["apiVersion"] == (
        "external-secrets.io/v1alpha1"
    )


def test_body_selects_the_generated_secret() -> None:
    body = _build()
    assert body["spec"]["selector"] == {
        "secret": {"name": "orders-api-pg-credentials"}
    }


def test_all_keys_are_bundled_into_one_atomic_write() -> None:
    """The whole point: one remote object, one push, no read-modify-write.

    Per-key entries with `remoteRef.property` each re-read and rewrite the same
    remote object, so against an eventually consistent store they drop each
    other's keys.
    """
    spec = _build()["spec"]
    assert "data" not in spec
    assert len(spec["dataTo"]) == 1
    entry = spec["dataTo"][0]
    assert entry["remoteKey"] == "prod/team-a/orders-api"
    assert entry["storeRef"] == {"name": "aws-sm", "kind": "ClusterSecretStore"}
    # No `property` anywhere — that is what makes the push a single write.
    assert "property" not in repr(entry)


def test_keys_are_selected_by_an_anchored_pattern() -> None:
    """Unanchored would let `port` also select `export_port`."""
    pattern = _build()["spec"]["dataTo"][0]["match"]["regexp"]
    assert pattern == "^(?:username|password|uri)$"
    compiled = re.compile(pattern)
    assert [k for k in ("username", "password", "uri") if compiled.search(k)] == [
        "username",
        "password",
        "uri",
    ]
    assert not compiled.search("export_uri")
    assert not compiled.search("uri_args")


def test_keys_can_be_restricted() -> None:
    spec = PushSecretSpec.model_validate(
        {"secretStoreRefs": [{"name": "aws-sm"}], "keys": ["password"]}
    )
    assert _build(spec)["spec"]["dataTo"][0]["match"]["regexp"] == "^(?:password)$"


def test_regex_metacharacters_in_a_key_are_escaped() -> None:
    spec = PushSecretSpec.model_validate(
        {"secretStoreRefs": [{"name": "aws-sm"}], "keys": ["ca.crt"]}
    )
    pattern = _build(spec)["spec"]["dataTo"][0]["match"]["regexp"]
    assert re.compile(pattern).search("ca.crt")
    # The dot must not act as a wildcard.
    assert not re.compile(pattern).search("caXcrt")


def test_one_data_to_entry_per_store() -> None:
    spec = PushSecretSpec.model_validate(
        {
            "secretStoreRefs": [
                {"name": "aws-sm"},
                {"name": "vault", "kind": "SecretStore"},
            ]
        }
    )
    entries = _build(spec)["spec"]["dataTo"]
    assert [e["storeRef"]["name"] for e in entries] == ["aws-sm", "vault"]
    assert [e["storeRef"]["kind"] for e in entries] == [
        "ClusterSecretStore",
        "SecretStore",
    ]


def test_conversion_strategy_is_emitted_so_steady_state_is_a_no_op() -> None:
    """The CRD defaults this; omitting it means the read-back never compares
    equal and every reconcile re-patches."""
    assert _build()["spec"]["dataTo"][0]["conversionStrategy"] == "None"


def test_deletion_policy_defaults_to_leaving_the_remote_value() -> None:
    """Matches the RETAIN default: removing the CR must not destroy a secret."""
    assert _build()["spec"]["deletionPolicy"] == "None"


def test_refresh_interval_is_omitted_unless_set() -> None:
    assert "refreshInterval" not in _build()["spec"]
    spec = PushSecretSpec.model_validate(
        {"secretStoreRefs": [{"name": "aws-sm"}], "refreshInterval": "1h"}
    )
    assert _build(spec)["spec"]["refreshInterval"] == "1h"


def test_store_kind_defaults_to_cluster_scope() -> None:
    spec = PushSecretSpec.model_validate({"secretStoreRefs": [{"name": "aws-sm"}]})
    assert _build(spec)["spec"]["secretStoreRefs"] == [
        {"name": "aws-sm", "kind": "ClusterSecretStore"}
    ]


def test_labels_and_annotations_are_merged() -> None:
    spec = PushSecretSpec.model_validate(
        {
            "secretStoreRefs": [{"name": "aws-sm"}],
            "labels": {"team": "payments"},
            "annotations": {"note": "generated"},
        }
    )
    body = _build(spec)
    assert body["metadata"]["labels"]["team"] == "payments"
    assert body["metadata"]["labels"]["app.kubernetes.io/managed-by"] == "pg-operator"
    assert body["metadata"]["annotations"]["note"] == "generated"


def test_data_entries_carry_no_metadata_unless_configured() -> None:
    """Omitted, not empty: an unused envelope would be drift on every entry."""
    assert all("metadata" not in entry for entry in _build()["spec"]["dataTo"])


def test_provider_metadata_is_wrapped_in_a_pushsecretmetadata_envelope() -> None:
    spec = PushSecretSpec.model_validate(
        {
            "secretStoreRefs": [{"name": "aws-sm"}],
            "metadata": {
                "secretPushFormat": "string",
                "description": "managed by pg-operator",
                "tags": {"env": "prod"},
            },
        }
    )
    entry = _build(spec)["spec"]["dataTo"][0]
    assert entry["metadata"] == {
        "apiVersion": "kubernetes.external-secrets.io/v1alpha1",
        "kind": "PushSecretMetadata",
        # Passed through verbatim: only the provider knows this shape.
        "spec": {
            "secretPushFormat": "string",
            "description": "managed by pg-operator",
            "tags": {"env": "prod"},
        },
    }


def test_metadata_is_copied_per_entry_and_not_shared_with_the_spec() -> None:
    """Entries must not alias each other, or the CR's own metadata dict."""
    source = {"tags": {"env": "prod"}}
    spec = PushSecretSpec.model_validate(
        {
            "secretStoreRefs": [{"name": "aws-sm"}, {"name": "vault"}],
            "metadata": source,
        }
    )
    entries = _build(spec)["spec"]["dataTo"]
    entries[0]["metadata"]["spec"]["tags"]["env"] = "mutated"
    assert entries[1]["metadata"]["spec"]["tags"]["env"] == "prod"
    assert spec.metadata["tags"]["env"] == "prod"
    assert source == {"tags": {"env": "prod"}}


async def test_upgrade_clears_the_legacy_per_property_data_field() -> None:
    """A JSON merge patch only deletes a field when it is explicitly null.

    Without the null, a PushSecret written by an older operator would keep its
    per-property `data` entries alongside the new `dataTo` and external-secrets
    would push both — reintroducing the read-modify-write that loses keys.
    """
    k8s = FakeK8sClient()
    body = _build()
    legacy = {
        "apiVersion": body["apiVersion"],
        "kind": body["kind"],
        "metadata": dict(body["metadata"]),
        "spec": {
            "selector": {"secret": {"name": "orders-api-pg-credentials"}},
            "data": [
                {"match": {"secretKey": "username", "remoteRef": {"property": "u"}}}
            ],
        },
    }
    k8s.custom.objects[("pushsecrets", "team-a", "orders-api-pg-push")] = legacy

    created, changed = await apply_push_secret(k8s, Settings(), body)  # type: ignore[arg-type]

    assert (created, changed) == (False, True)
    stored = k8s.custom.objects[("pushsecrets", "team-a", "orders-api-pg-push")]
    assert stored["spec"]["data"] is None
    assert stored["spec"]["dataTo"] == body["spec"]["dataTo"]


async def test_a_converged_push_secret_is_left_alone() -> None:
    """Steady state must be a genuine no-op, not a patch every reconcile."""
    k8s = FakeK8sClient()
    body = _build()
    k8s.custom.objects[("pushsecrets", "team-a", "orders-api-pg-push")] = {
        "metadata": dict(body["metadata"]),
        "spec": body["spec"],
    }

    created, changed = await apply_push_secret(k8s, Settings(), body)  # type: ignore[arg-type]

    assert (created, changed) == (False, False)


@pytest.mark.parametrize(
    ("template", "expected"),
    [
        ("prod/{{ .namespace }}/{{ .name }}", "prod/team-a/orders-api"),
        ("prod/{{namespace}}/{{name}}", "prod/team-a/orders-api"),
        ("db/{{ .username }}", "db/orders_api"),
        ("literal/path", "literal/path"),
    ],
)
def test_remote_key_templating(template: str, expected: str) -> None:
    assert (
        render_remote_key(
            template, namespace="team-a", name="orders-api", username="orders_api"
        )
        == expected
    )
