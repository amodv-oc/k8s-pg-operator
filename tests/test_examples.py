"""Every shipped example must be accepted by the models and the CRD schemas.

Examples are the first thing anyone copies, so a stale field or a typo in one is
a real defect. Validating them here means documentation cannot drift from the
code without a test failing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from pg_operator.constants import API_GROUP, API_VERSION
from pg_operator.models import parse_database_spec, parse_instance_spec, parse_user_spec

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"

PARSERS = {
    "PostgresInstance": parse_instance_spec,
    "PostgresDB": parse_database_spec,
    "PostgresUser": parse_user_spec,
}


def documents() -> list[tuple[str, dict[str, Any]]]:
    found: list[tuple[str, dict[str, Any]]] = []
    for path in sorted(EXAMPLES.glob("*.yaml")):
        for index, doc in enumerate(yaml.safe_load_all(path.read_text())):
            if doc:
                found.append((f"{path.name}[{index}]", doc))
    return found


ALL = documents()
OURS = [(name, doc) for name, doc in ALL if doc.get("kind") in PARSERS]


def test_examples_exist() -> None:
    assert OURS, "no operator resources found in examples/"


@pytest.mark.parametrize(("name", "doc"), OURS, ids=[n for n, _ in OURS])
def test_example_spec_is_valid(name: str, doc: dict[str, Any]) -> None:
    """The strict models reject unknown fields, so this catches typos too."""
    del name
    PARSERS[doc["kind"]](doc["spec"])


@pytest.mark.parametrize(("name", "doc"), OURS, ids=[n for n, _ in OURS])
def test_example_uses_the_current_api_version(name: str, doc: dict[str, Any]) -> None:
    del name
    assert doc["apiVersion"] == f"{API_GROUP}/{API_VERSION}"


@pytest.mark.parametrize(("name", "doc"), OURS, ids=[n for n, _ in OURS])
def test_namespaced_examples_declare_a_namespace(
    name: str, doc: dict[str, Any]
) -> None:
    """PostgresInstance is cluster-scoped; the other two are not."""
    del name
    metadata = doc["metadata"]
    if doc["kind"] == "PostgresInstance":
        assert "namespace" not in metadata
    else:
        assert metadata.get("namespace"), "namespaced example without a namespace"


def test_every_kind_is_demonstrated() -> None:
    demonstrated = {doc["kind"] for _, doc in OURS}
    assert demonstrated == set(PARSERS)


def test_examples_cover_all_three_access_levels() -> None:
    levels = {
        entry["role"]
        for _, doc in OURS
        if doc["kind"] == "PostgresUser"
        for entry in doc["spec"].get("access", [])
    }
    assert levels == {"OWNER", "RW", "RO"}


def test_examples_cover_both_retention_policies() -> None:
    policies = {
        doc["spec"]["retentionPolicy"]
        for _, doc in OURS
        if "retentionPolicy" in doc["spec"]
    }
    assert policies == {"RETAIN", "DROP"}


def test_referenced_databases_exist_in_the_examples() -> None:
    """A dbRef pointing at nothing would fail on a real cluster."""
    databases = {
        (doc["metadata"]["namespace"], doc["metadata"]["name"])
        for _, doc in OURS
        if doc["kind"] == "PostgresDB"
    }
    for name, doc in OURS:
        if doc["kind"] != "PostgresUser":
            continue
        namespace = doc["metadata"]["namespace"]
        for entry in doc["spec"].get("access", []):
            ref = entry["dbRef"]
            key = (ref.get("namespace", namespace), ref["name"])
            assert key in databases, f"{name} references unknown PostgresDB {key}"


def test_referenced_instances_exist_in_the_examples() -> None:
    instances = {
        doc["metadata"]["name"] for _, doc in OURS if doc["kind"] == "PostgresInstance"
    }
    for name, doc in OURS:
        if doc["kind"] == "PostgresInstance":
            continue
        referenced = doc["spec"]["instanceRef"]["name"]
        assert referenced in instances, f"{name} references unknown instance {referenced}"


def test_no_example_contains_a_literal_password() -> None:
    """Examples must reference credentials, never carry them."""
    for path in sorted(EXAMPLES.glob("*.yaml")):
        text = path.read_text()
        for marker in ("password: ", "PGPASSWORD: "):
            for line in text.splitlines():
                stripped = line.strip()
                if stripped.startswith(marker):
                    # Only key *names* and refs are allowed, not values.
                    value = stripped.split(":", 1)[1].strip()
                    assert value in {"password", "PGPASSWORD", ""}, (
                        f"{path.name} appears to contain a literal secret: {stripped}"
                    )
