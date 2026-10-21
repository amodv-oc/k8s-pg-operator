"""Consistency between the CRD schemas and the operator's own models.

A field the model accepts but the CRD omits is rejected by the API server
before the operator ever sees it; a field the CRD accepts but the model omits is
rejected by the operator as an unknown field. Neither shows up in a unit test of
either half alone, so they are compared here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, get_args, get_origin

import pytest
import yaml
from pydantic import BaseModel

from pg_operator.constants import API_GROUP, API_VERSION
from pg_operator.models import DatabaseSpec, InstanceSpec, ReplicationSpec, UserSpec

CRD_DIR = Path(__file__).resolve().parents[1] / "charts" / "pg-operator" / "crds"

CASES = [
    ("postgresinstances.yaml", "PostgresInstance", InstanceSpec, "Cluster"),
    ("postgresdbs.yaml", "PostgresDB", DatabaseSpec, "Namespaced"),
    ("postgresusers.yaml", "PostgresUser", UserSpec, "Namespaced"),
    ("postgresreplications.yaml", "PostgresReplication", ReplicationSpec, "Namespaced"),
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


def test_logical_replication_status_matches_the_probe() -> None:
    """Every key the probe writes is declared, and the enums agree with its types."""
    from typing import get_args

    from pg_operator.postgres.replication import (
        LogicalReplicationInfo,
        ReplicationGrant,
        SubscriptionPrivilege,
    )

    block = load("postgresinstances.yaml")["spec"]["versions"][0]["schema"][
        "openAPIV3Schema"
    ]["properties"]["status"]["properties"]["logicalReplication"]["properties"]
    written = LogicalReplicationInfo(
        major_version=16,
        current_user="pgadmin",
        wal_level="logical",
        max_replication_slots=10,
        replication_slots_in_use=0,
        max_wal_senders=10,
        max_slot_wal_keep_size="-1",
        create_subscription="Granted",
        replication_grant="rds_replication",
        is_rds=True,
    ).as_status()
    assert set(written) == set(block)
    assert block["createSubscription"]["enum"] == list(get_args(SubscriptionPrivilege))
    assert block["replicationGrant"]["enum"] == list(get_args(ReplicationGrant))


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


# --- PostgresReplication ----------------------------------------------------
#
# Its spec nests four levels deep, where the top-level comparisons above cannot
# see, so the model and the CRD are walked together at every depth.


def _spec_model(annotation: Any) -> type[BaseModel] | None:
    """The Spec model a field holds, through Optional and list, if any."""
    for candidate in (annotation, *get_args(annotation)):
        if isinstance(candidate, type) and issubclass(candidate, BaseModel):
            return candidate
        if get_origin(candidate) is list:
            nested = _spec_model(get_args(candidate)[0])
            if nested is not None:
                return nested
    return None


def _walk(model: type[BaseModel], schema: dict[str, Any], path: str) -> list[str]:
    problems: list[str] = []
    properties = schema.get("properties") or {}
    fields = {(field.alias or name): field for name, field in model.model_fields.items()}
    for missing in sorted(set(fields) - set(properties)):
        problems.append(f"{path}.{missing}: in the model, not the CRD")
    for unknown in sorted(set(properties) - set(fields)):
        problems.append(f"{path}.{unknown}: in the CRD, not the model")
    for wire, field in fields.items():
        nested = _spec_model(field.annotation)
        if nested is None or wire not in properties:
            continue
        child = properties[wire]
        child = child.get("items", child) if child.get("type") == "array" else child
        problems.extend(_walk(nested, child, f"{path}.{wire}"))
    return problems


def replication_spec_schema() -> dict[str, Any]:
    version = load("postgresreplications.yaml")["spec"]["versions"][0]
    return version["schema"]["openAPIV3Schema"]["properties"]["spec"]


def test_replication_crd_matches_the_model_at_every_depth() -> None:
    assert _walk(ReplicationSpec, replication_spec_schema(), "spec") == []


def test_replication_enums_match_the_model() -> None:
    from pg_operator.models import FilterOperator, PublishOperation, SubscriptionStreaming

    spec = replication_spec_schema()["properties"]
    predicate = spec["tables"]["items"]["properties"]["rowFilter"]["properties"]["all"]["items"]
    assert predicate["properties"]["op"]["enum"] == list(get_args(FilterOperator))
    assert spec["publish"]["items"]["enum"] == list(get_args(PublishOperation))
    streaming = spec["subscription"]["properties"]["streaming"]["enum"]
    # A YAML 1.1 parser reads bare on/off as booleans; the CRD must quote them.
    assert streaming == list(get_args(SubscriptionStreaming))
    assert all(isinstance(value, str) for value in streaming)


def test_replication_defaults_match_the_model() -> None:
    spec = replication_spec_schema()["properties"]
    model = ReplicationSpec.model_validate(
        {"source": {"dbRef": {"name": "a"}}, "target": {"dbRef": {"name": "b"}}, "schemas": [{"name": "public"}]}
    )
    assert spec["publish"]["default"] == model.publish
    alerting = spec["alerting"]["properties"]
    assert alerting["maxLag"]["default"] == model.alerting.max_lag
    assert alerting["maxRetainedWal"]["default"] == model.alerting.max_retained_wal
    assert alerting["maxSlotInactive"]["default"] == model.alerting.max_slot_inactive
    assert spec["source"]["properties"]["connection"]["properties"]["sslMode"]["default"] == (
        model.source.connection.ssl_mode
    )


def test_replication_quantity_and_duration_patterns_match_the_parsers() -> None:
    from pg_operator.units import DURATION_PATTERN, QUANTITY_PATTERN

    alerting = replication_spec_schema()["properties"]["alerting"]["properties"]
    assert alerting["maxLag"]["pattern"] == QUANTITY_PATTERN
    assert alerting["maxRetainedWal"]["pattern"] == QUANTITY_PATTERN
    assert alerting["maxSlotInactive"]["pattern"] == DURATION_PATTERN


def test_replication_ends_are_immutable() -> None:
    """Re-pointing a replication would orphan its publication, slot or subscription."""
    spec = replication_spec_schema()["properties"]
    for side in ("source", "target"):
        db_ref = spec[side]["properties"]["dbRef"]
        assert any(rule["rule"] == "self == oldSelf" for rule in db_ref["x-kubernetes-validations"])


def test_replication_selections_are_keyed_maps() -> None:
    """The API server then rejects a duplicate schema or table itself."""
    spec = replication_spec_schema()["properties"]
    assert spec["schemas"]["x-kubernetes-list-type"] == "map"
    assert spec["schemas"]["x-kubernetes-list-map-keys"] == ["name"]
    assert spec["tables"]["x-kubernetes-list-type"] == "map"
    assert spec["tables"]["x-kubernetes-list-map-keys"] == ["schema", "name"]
    for key in spec["tables"]["x-kubernetes-list-map-keys"]:
        assert key in spec["tables"]["items"]["required"]


def test_replication_admission_rules_cover_what_the_model_refuses() -> None:
    """Each cross-field rule the model enforces has a CEL counterpart.

    CEL cannot be evaluated here; `make local-up` proves the rules compile and
    bite. This proves none has been dropped.
    """
    spec = replication_spec_schema()
    connection = spec["properties"]["source"]["properties"]["connection"]
    row_filter = spec["properties"]["tables"]["items"]["properties"]["rowFilter"]
    predicate = row_filter["properties"]["all"]["items"]

    def rules(node: dict[str, Any]) -> str:
        return " ".join(rule["rule"] for rule in node.get("x-kubernetes-validations", []))

    assert "size(self.schemas)" in rules(spec) and "size(self.tables)" in rules(spec)
    assert "sslRootCertPath" in rules(connection)
    assert "has(self.all) != has(self.any)" in rules(row_filter)
    assert "'In'" in rules(predicate) and "IsNull" in rules(predicate)


def _schemas_with_any_of(node: Any, path: str = "") -> list[tuple[str, dict[str, Any]]]:
    found: list[tuple[str, dict[str, Any]]] = []
    if isinstance(node, dict):
        if "anyOf" in node:
            found.append((path, node))
        for key, value in node.items():
            found.extend(_schemas_with_any_of(value, f"{path}.{key}"))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found.extend(_schemas_with_any_of(value, f"{path}[{index}]"))
    return found


@pytest.mark.parametrize(("filename", "kind", "model", "scope"), CASES)
def test_any_of_is_only_the_structural_int_or_string_form(
    filename: str, kind: str, model: type, scope: str
) -> None:
    """The API server refuses a CRD whose anyOf branches carry anything but a type.

    `minimum: 1` inside the integer branch made the whole CRD non-structural -
    found only by `make local-up`. Constraints belong beside the anyOf, or in CEL.
    """
    del kind, model, scope
    for path, node in _schemas_with_any_of(load(filename)):
        assert node["anyOf"] == [{"type": "integer"}, {"type": "string"}], path
        assert node.get("x-kubernetes-int-or-string") is True, path
