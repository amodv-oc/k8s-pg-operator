"""Consistency between the CRD schemas and the operator's own models.

A field the model accepts but the CRD omits is rejected by the API server
before the operator ever sees it; a field the CRD accepts but the model omits is
rejected by the operator as an unknown field. Neither shows up in a unit test of
either half alone, so they are compared here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from pg_operator.constants import API_GROUP, API_VERSION
from pg_operator.models import DatabaseSpec, InstanceSpec, UserSpec

CRD_DIR = Path(__file__).resolve().parents[1] / "charts" / "pg-operator" / "crds"

CASES = [
    ("postgresinstances.yaml", "PostgresInstance", InstanceSpec, "Cluster"),
    ("postgresdbs.yaml", "PostgresDB", DatabaseSpec, "Namespaced"),
    ("postgresusers.yaml", "PostgresUser", UserSpec, "Namespaced"),
]


def load(filename: str) -> dict[str, Any]:
    return yaml.safe_load((CRD_DIR / filename).read_text())


def model_field_names(model: type) -> set[str]:
    """The names the model accepts on the wire (alias where one is set)."""
    return {
        field.alias or name for name, field in model.model_fields.items()
    }


def crd_spec_properties(crd: dict[str, Any]) -> dict[str, Any]:
    version = crd["spec"]["versions"][0]
    return version["schema"]["openAPIV3Schema"]["properties"]["spec"]["properties"]


@pytest.mark.parametrize(("filename", "kind", "model", "scope"), CASES)
def test_crd_metadata(filename: str, kind: str, model: type, scope: str) -> None:
    del model
    crd = load(filename)
    assert crd["apiVersion"] == "apiextensions.k8s.io/v1"
    assert crd["spec"]["group"] == API_GROUP
    assert crd["spec"]["scope"] == scope
    assert crd["spec"]["names"]["kind"] == kind
    assert crd["metadata"]["name"] == f"{crd['spec']['names']['plural']}.{API_GROUP}"


@pytest.mark.parametrize(("filename", "kind", "model", "scope"), CASES)
def test_version_is_served_and_stored_with_a_status_subresource(
    filename: str, kind: str, model: type, scope: str
) -> None:
    del kind, model, scope
    versions = load(filename)["spec"]["versions"]
    assert len(versions) == 1
    version = versions[0]
    assert version["name"] == API_VERSION
    assert version["served"] is True
    assert version["storage"] is True
    # The operator writes status through the subresource.
    assert "status" in version["subresources"]


@pytest.mark.parametrize(("filename", "kind", "model", "scope"), CASES)
def test_every_model_field_exists_in_the_crd(
    filename: str, kind: str, model: type, scope: str
) -> None:
    del kind, scope
    properties = crd_spec_properties(load(filename))
    missing = model_field_names(model) - set(properties)
    assert not missing, f"{filename} is missing spec properties: {sorted(missing)}"


@pytest.mark.parametrize(("filename", "kind", "model", "scope"), CASES)
def test_every_crd_field_exists_in_the_model(
    filename: str, kind: str, model: type, scope: str
) -> None:
    """The models forbid extra fields, so a CRD-only field would be rejected."""
    del kind, scope
    properties = crd_spec_properties(load(filename))
    unknown = set(properties) - model_field_names(model)
    assert not unknown, f"{filename} declares fields the model rejects: {sorted(unknown)}"


@pytest.mark.parametrize(("filename", "kind", "model", "scope"), CASES)
def test_required_fields_agree(filename: str, kind: str, model: type, scope: str) -> None:
    del kind, scope
    version = load(filename)["spec"]["versions"][0]
    declared = set(version["schema"]["openAPIV3Schema"]["properties"]["spec"].get("required", []))
    model_required = {
        field.alias or name
        for name, field in model.model_fields.items()
        if field.is_required()
    }
    assert declared == model_required, (
        f"{filename}: CRD requires {sorted(declared)}, model requires "
        f"{sorted(model_required)}"
    )


@pytest.mark.parametrize(("filename", "kind", "model", "scope"), CASES)
def test_printer_columns_use_scalar_json_paths(
    filename: str, kind: str, model: type, scope: str
) -> None:
    """An array jsonPath renders as <none> in kubectl, so counts are used."""
    del kind, model, scope
    columns = load(filename)["spec"]["versions"][0]["additionalPrinterColumns"]
    assert columns
    for column in columns:
        assert column["type"] in {"string", "integer", "boolean", "date", "number"}
        assert column["jsonPath"].startswith(".")


def test_retention_policy_is_an_enum_defaulting_to_retain() -> None:
    instance = crd_spec_properties(load("postgresinstances.yaml"))["retentionPolicy"]
    assert instance["enum"] == ["RETAIN", "DROP"]
    assert instance["default"] == "RETAIN"

    # Children have no default: an unset value inherits the instance's policy.
    for filename in ("postgresdbs.yaml", "postgresusers.yaml"):
        child = crd_spec_properties(load(filename))["retentionPolicy"]
        assert child["enum"] == ["RETAIN", "DROP"]
        assert "default" not in child


def test_access_levels_match_the_operator_constants() -> None:
    from pg_operator.constants import ACCESS_LEVELS

    access = crd_spec_properties(load("postgresusers.yaml"))["access"]
    assert access["items"]["properties"]["role"]["enum"] == list(ACCESS_LEVELS)


def test_immutable_fields_carry_a_cel_rule() -> None:
    """Renaming a database or role would orphan the object it points at."""
    database_name = crd_spec_properties(load("postgresdbs.yaml"))["databaseName"]
    assert any(
        rule["rule"] == "self == oldSelf"
        for rule in database_name["x-kubernetes-validations"]
    )
    username = crd_spec_properties(load("postgresusers.yaml"))["username"]
    assert any(
        rule["rule"] == "self == oldSelf"
        for rule in username["x-kubernetes-validations"]
    )


def test_identifier_fields_are_pattern_constrained() -> None:
    """Defence in depth: the API server rejects unquotable names too."""
    pattern = "^[a-z_][a-z0-9_$]*$"
    assert crd_spec_properties(load("postgresdbs.yaml"))["databaseName"]["pattern"] == pattern
    assert crd_spec_properties(load("postgresusers.yaml"))["username"]["pattern"] == pattern


def test_schema_and_extension_lists_are_keyed_maps() -> None:
    """Server-side apply merges by name instead of replacing the whole list."""
    properties = crd_spec_properties(load("postgresdbs.yaml"))
    for field in ("schemas", "extensions"):
        assert properties[field]["x-kubernetes-list-type"] == "map"
        assert properties[field]["x-kubernetes-list-map-keys"] == ["name"]


def test_ssl_verification_rule_is_enforced_by_the_api_server() -> None:
    spec = load("postgresinstances.yaml")["spec"]["versions"][0]["schema"][
        "openAPIV3Schema"
    ]["properties"]["spec"]
    rules = [rule["rule"] for rule in spec["x-kubernetes-validations"]]
    assert any("sslRootCertSecretRef" in rule for rule in rules)


@pytest.mark.parametrize(("filename", "kind", "model", "scope"), CASES)
def test_status_conditions_follow_the_kubernetes_shape(
    filename: str, kind: str, model: type, scope: str
) -> None:
    del kind, model, scope
    version = load(filename)["spec"]["versions"][0]
    conditions = version["schema"]["openAPIV3Schema"]["properties"]["status"][
        "properties"
    ]["conditions"]
    properties = conditions["items"]["properties"]
    assert set(conditions["items"]["required"]) == {"type", "status"}
    for field in ("type", "status", "reason", "message", "lastTransitionTime"):
        assert field in properties
