"""PushSecret body construction and remote-key templating."""

from __future__ import annotations

import pytest

from pg_operator.k8s.pushsecret import (
    build_push_secret,
    render_remote_key,
    split_api_version,
)
from pg_operator.models import PushSecretSpec

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


def test_every_secret_key_becomes_a_property_of_one_remote_object() -> None:
    data = _build()["spec"]["data"]
    assert [entry["match"]["secretKey"] for entry in data] == [
        "username",
        "password",
        "uri",
    ]
    # One remote object, each Kubernetes key a property inside it.
    assert {entry["match"]["remoteRef"]["remoteKey"] for entry in data} == {
        "prod/team-a/orders-api"
    }
    assert [entry["match"]["remoteRef"]["property"] for entry in data] == [
        "username",
        "password",
        "uri",
    ]


def test_keys_can_be_restricted() -> None:
    spec = PushSecretSpec.model_validate(
        {"secretStoreRefs": [{"name": "aws-sm"}], "keys": ["password"]}
    )
    data = _build(spec)["spec"]["data"]
    assert [entry["match"]["secretKey"] for entry in data] == ["password"]


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
