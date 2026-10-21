"""The cluster harness embeds the same SQL the integration suite runs.

``hack/local/00-servers.yaml`` must stay self-contained (``make local-down``
deletes exactly what it applied), so it carries copies of the bootstrap and the
``rds_replication`` shim rather than generating them. These tests keep the
copies identical, so the cluster and the integration suite cannot drift into
emulating two different RDSes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
SERVERS = ROOT / "hack" / "local" / "00-servers.yaml"


def _documents() -> list[dict[str, Any]]:
    return [doc for doc in yaml.safe_load_all(SERVERS.read_text()) if doc]


def _configmap(name: str) -> dict[str, str]:
    for doc in _documents():
        if doc["kind"] == "ConfigMap" and doc["metadata"]["name"] == name:
            return doc["data"]
    raise AssertionError(f"ConfigMap {name} not found in {SERVERS.name}")


def _deployments() -> dict[str, dict[str, Any]]:
    return {
        doc["metadata"]["name"]: doc for doc in _documents() if doc["kind"] == "Deployment"
    }


def test_bootstrap_is_the_integration_suite_bootstrap() -> None:
    bootstrap = _configmap("rds-bootstrap")
    assert bootstrap["10-rds-like.sql"] == (ROOT / "hack" / "rds-like.sql").read_text()
    # The one deliberate difference, and it must run after the roles exist.
    assert sorted(bootstrap) == ["10-rds-like.sql", "20-set-parameter.sql"]


def test_shim_is_the_integration_suite_shim() -> None:
    shim = _configmap("rds-replication-shim")["rds-replication-shim.sql"]
    assert shim == (ROOT / "hack" / "rds-replication-shim.sql").read_text()


def test_only_servers_that_support_replication_are_logical_and_shimmed() -> None:
    for name, deployment in _deployments().items():
        containers = deployment["spec"]["template"]["spec"]["containers"]
        postgres = next(c for c in containers if c["name"] == "postgres")
        logical = "wal_level=logical" in postgres["args"]
        shimmed = any(c["name"] == "rds-replication-shim" for c in containers)
        supports = int(name.removeprefix("pg")) >= 16
        assert logical is supports, name
        assert shimmed is supports, name


def test_exec_into_a_server_still_reaches_postgres() -> None:
    """verify.sh runs `kubectl exec deploy/pgNN` and compares what it prints.

    With a sidecar in the pod, kubectl would otherwise pick a container itself
    and say so on stderr - which verify.sh captures and fails on.
    """
    for name, deployment in _deployments().items():
        template = deployment["spec"]["template"]
        if len(template["spec"]["containers"]) > 1:
            annotations = template["metadata"].get("annotations") or {}
            assert annotations.get("kubectl.kubernetes.io/default-container") == "postgres", name
