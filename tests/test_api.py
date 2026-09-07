"""State API: resource projection, filtering and failure behaviour.

The store is exercised against a fake Kubernetes layer so the projection logic
is tested without a cluster. The API is read-only, so the most important
assertions are about what it reports and what it must never expose.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from pg_operator.api.main import create_app
from pg_operator.api.store import StateStore
from pg_operator.config import Settings


def instance_obj(name: str, phase: str = "Ready", **status: Any) -> dict[str, Any]:
    return {
        "apiVersion": "postgres.opsplatform.io/v1alpha1",
        "kind": "PostgresInstance",
        "metadata": {"name": name, "generation": 2, "creationTimestamp": "2026-01-01T00:00:00Z"},
        "spec": {
            "host": f"{name}.rds.amazonaws.com",
            "port": 5432,
            "sslMode": "require",
            "credentialsSecretRef": {"name": f"{name}-super", "namespace": "pg-operator"},
        },
        "status": {
            "phase": phase,
            "endpoint": f"{name}.rds.amazonaws.com:5432",
            "serverVersion": "16.3",
            "managingRole": "postgres",
            "privileges": "rds_superuser, CREATEDB, CREATEROLE",
            "credentialsSecret": f"pg-operator/{name}-super",
            "databases": 1,
            "users": 2,
            "observedGeneration": 2,
            "conditions": [
                {"type": "Ready", "status": "True", "reason": "Reconciled", "message": "ok"}
            ],
            **status,
        },
    }


def database_obj(
    name: str, namespace: str = "team-a", phase: str = "Ready", **status: Any
) -> dict[str, Any]:
    return {
        "apiVersion": "postgres.opsplatform.io/v1alpha1",
        "kind": "PostgresDB",
        "metadata": {"name": name, "namespace": namespace, "generation": 1},
        "spec": {"instanceRef": {"name": "prod-rds"}, "schemas": [{"name": "audit"}]},
        "status": {
            "phase": phase,
            "instance": "prod-rds",
            "databaseName": name.replace("-", "_"),
            "retentionPolicy": "RETAIN",
            "roles": {
                "owner": f"{name}_owner".replace("-", "_"),
                "readWrite": f"{name}_rw".replace("-", "_"),
                "readOnly": f"{name}_ro".replace("-", "_"),
            },
            "managedSchemas": ["public", "audit"],
            "observedSchemas": ["public", "audit"],
            "orphanedSchemas": [],
            "conditions": [
                {"type": "Ready", "status": "True", "reason": "Reconciled", "message": "in sync"}
            ],
            **status,
        },
    }


def user_obj(
    name: str,
    namespace: str = "team-a",
    grants: list[dict[str, str]] | None = None,
    phase: str = "Ready",
    **spec: Any,
) -> dict[str, Any]:
    return {
        "apiVersion": "postgres.opsplatform.io/v1alpha1",
        "kind": "PostgresUser",
        "metadata": {"name": name, "namespace": namespace, "generation": 1},
        "spec": {"instanceRef": {"name": "prod-rds"}, **spec},
        "status": {
            "phase": phase,
            "instance": "prod-rds",
            "username": name.replace("-", "_"),
            "grants": grants if grants is not None else [],
            "secretRef": {"name": f"{name}-pg-credentials", "namespace": namespace},
            "secretKeys": ["username", "password", "uri"],
            "conditions": [
                {"type": "Ready", "status": "True", "reason": "Reconciled", "message": "ok"}
            ],
        },
    }


class FakeCustomObjects:
    """Stands in for CustomObjectsApi, keyed by plural."""

    def __init__(self, items: dict[str, list[dict[str, Any]]]) -> None:
        self.items = items
        self.calls = 0

    async def list_cluster_custom_object(
        self, group: str, version: str, plural: str, **_: Any
    ) -> dict[str, Any]:
        del group, version
        self.calls += 1
        return {"items": list(self.items.get(plural, []))}

    async def list_namespaced_custom_object(
        self, group: str, version: str, namespace: str, plural: str, **_: Any
    ) -> dict[str, Any]:
        del group, version
        self.calls += 1
        return {
            "items": [
                item
                for item in self.items.get(plural, [])
                if item["metadata"].get("namespace") == namespace
            ]
        }


class FakeK8s:
    def __init__(self, items: dict[str, list[dict[str, Any]]]) -> None:
        self.custom = FakeCustomObjects(items)


@pytest.fixture
def items() -> dict[str, list[dict[str, Any]]]:
    return {
        "postgresinstances": [instance_obj("prod-rds"), instance_obj("dev-rds", "Unreachable")],
        "postgresdbs": [
            database_obj("orders"),
            database_obj(
                "reporting",
                phase="Drifted",
                orphanedSchemas=["legacy"],
                conditions=[
                    {"type": "Ready", "status": "True", "reason": "Reconciled", "message": "ok"},
                    {
                        "type": "Drifted",
                        "status": "True",
                        "reason": "RetainedOrphans",
                        "message": "schema 'legacy' retained",
                    },
                ],
            ),
            database_obj("billing", namespace="team-b"),
        ],
        "postgresusers": [
            user_obj(
                "orders-api",
                grants=[
                    {
                        "database": "orders",
                        "role": "RW",
                        "groupRole": "orders_rw",
                        "dbRef": "team-a/orders",
                    }
                ],
            ),
            user_obj(
                "analytics",
                grants=[
                    {
                        "database": "orders",
                        "role": "RO",
                        "groupRole": "orders_ro",
                        "dbRef": "team-a/orders",
                    }
                ],
            ),
            user_obj("billing-api", namespace="team-b", phase="Failed"),
        ],
    }


@pytest.fixture
def store(items: dict[str, list[dict[str, Any]]]) -> StateStore:
    # TTL 0 so each test observes fresh reads rather than a cached page.
    return StateStore(FakeK8s(items), Settings(api_cache_ttl=0))  # type: ignore[arg-type]


@pytest.fixture
def client(store: StateStore) -> TestClient:
    app = create_app()

    class Provider:
        async def store(self) -> StateStore:
            return store

        async def close(self) -> None:
            return None

    with TestClient(app) as test_client:
        test_client.app.state.provider = Provider()  # type: ignore[attr-defined]
        yield test_client


# --- projection ------------------------------------------------------------


async def test_instances_are_projected_from_status(store: StateStore) -> None:
    instances = {item.name: item for item in await store.instances()}
    prod = instances["prod-rds"]
    assert prod.phase == "Ready"
    assert prod.server_version == "16.3"
    assert prod.endpoint == "prod-rds.rds.amazonaws.com:5432"
    assert prod.privileges == "rds_superuser, CREATEDB, CREATEROLE"
    assert prod.message == "ok"
    assert instances["dev-rds"].phase == "Unreachable"


async def test_endpoint_falls_back_to_the_spec_before_first_reconcile(
    store: StateStore,
) -> None:
    obj = instance_obj("new-rds")
    obj["status"] = {}
    store._k8s.custom.items["postgresinstances"] = [obj]  # type: ignore[attr-defined]
    instance = await store.instance("new-rds")
    assert instance is not None
    assert instance.phase == "Unknown"
    assert instance.endpoint == "new-rds.rds.amazonaws.com:5432"


async def test_database_group_roles_are_projected(store: StateStore) -> None:
    orders = await store.database("team-a", "orders")
    assert orders is not None
    assert orders.roles.owner == "orders_owner"
    assert orders.roles.read_write == "orders_rw"
    assert orders.roles.read_only == "orders_ro"
    assert orders.managed_schemas == ["public", "audit"]


async def test_user_grants_are_projected(store: StateStore) -> None:
    user = await store.user("team-a", "orders-api")
    assert user is not None
    assert user.username == "orders_api"
    assert [(g.database, g.role) for g in user.grants] == [("orders", "RW")]
    assert user.secret_ref is not None
    assert user.secret_ref.name == "orders-api-pg-credentials"


async def test_byo_password_is_flagged_without_reading_it(store: StateStore) -> None:
    obj = user_obj("byo", passwordSecretRef={"name": "existing", "key": "password"})
    store._k8s.custom.items["postgresusers"] = [obj]  # type: ignore[attr-defined]
    user = await store.user("team-a", "byo")
    assert user is not None
    assert user.byo_password is True


async def test_unknown_phase_values_are_normalised(store: StateStore) -> None:
    obj = database_obj("weird", phase="SomethingElse")
    store._k8s.custom.items["postgresdbs"] = [obj]  # type: ignore[attr-defined]
    database = await store.database("team-a", "weird")
    assert database is not None
    assert database.phase == "Unknown"


# --- aggregates ------------------------------------------------------------


async def test_overview_counts_phases(store: StateStore) -> None:
    overview = await store.overview()
    assert overview.instances.total == 2
    assert overview.instances.ready == 1
    assert overview.instances.unreachable == 1
    assert overview.databases.drifted == 1
    assert overview.users.failed == 1


async def test_overview_lists_what_needs_attention(store: StateStore) -> None:
    overview = await store.overview()
    assert overview.healthy is False
    joined = "\n".join(overview.attention)
    assert "PostgresInstance/dev-rds is Unreachable" in joined
    assert "PostgresDB/team-a/reporting is Drifted" in joined
    assert "PostgresUser/team-b/billing-api is Failed" in joined


async def test_overview_is_healthy_when_everything_is_ready(
    items: dict[str, list[dict[str, Any]]],
) -> None:
    store = StateStore(
        FakeK8s(
            {
                "postgresinstances": [instance_obj("prod-rds")],
                "postgresdbs": [database_obj("orders")],
                "postgresusers": [user_obj("orders-api")],
            }
        ),  # type: ignore[arg-type]
        Settings(api_cache_ttl=0),
    )
    overview = await store.overview()
    assert overview.healthy is True
    assert overview.attention == []


async def test_retained_orphans_are_surfaced_even_when_ready(
    store: StateStore,
) -> None:
    """A RETAIN policy hides nothing: the orphan is reported, not silently kept."""
    obj = database_obj("kept", orphanedSchemas=["legacy"])
    store._k8s.custom.items["postgresdbs"] = [obj]  # type: ignore[attr-defined]
    overview = await store.overview()
    assert any("retains orphaned object(s): legacy" in msg for msg in overview.attention)


# --- caching ---------------------------------------------------------------


async def test_results_are_cached_within_the_ttl(
    items: dict[str, list[dict[str, Any]]],
) -> None:
    k8s = FakeK8s(items)
    store = StateStore(k8s, Settings(api_cache_ttl=60))  # type: ignore[arg-type]
    await store.instances()
    await store.instances()
    await store.instances()
    assert k8s.custom.calls == 1, "dashboard polling must not fan out to the API server"

    store.invalidate()
    await store.instances()
    assert k8s.custom.calls == 2


# --- endpoints -------------------------------------------------------------


def test_healthz_does_not_need_a_cluster(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_list_and_filter_databases(client: TestClient) -> None:
    assert len(client.get("/api/v1/databases").json()) == 3
    assert len(client.get("/api/v1/databases?namespace=team-b").json()) == 1
    assert len(client.get("/api/v1/databases?phase=drifted").json()) == 1
    assert len(client.get("/api/v1/databases?drifted=true").json()) == 1
    assert len(client.get("/api/v1/databases?instance=prod-rds").json()) == 3


def test_filter_users_by_access_level_and_database(client: TestClient) -> None:
    assert len(client.get("/api/v1/users?access=RW").json()) == 1
    assert len(client.get("/api/v1/users?access=ro").json()) == 1
    assert len(client.get("/api/v1/users?database=orders").json()) == 2
    assert len(client.get("/api/v1/users?namespace=team-b").json()) == 1


def test_instance_detail_includes_its_dependents(client: TestClient) -> None:
    body = client.get("/api/v1/instances/prod-rds").json()
    assert body["name"] == "prod-rds"
    assert len(body["database_list"]) == 3
    assert len(body["user_list"]) == 3


def test_database_detail_lists_the_users_granted_on_it(client: TestClient) -> None:
    body = client.get("/api/v1/databases/team-a/orders").json()
    assert {user["name"] for user in body["users"]} == {"orders-api", "analytics"}


def test_missing_resources_return_404(client: TestClient) -> None:
    assert client.get("/api/v1/instances/nope").status_code == 404
    assert client.get("/api/v1/databases/team-a/nope").status_code == 404
    assert client.get("/api/v1/users/team-a/nope").status_code == 404


def test_no_response_exposes_a_password(client: TestClient) -> None:
    """The API names the Secret and its keys; it never returns a value."""
    for path in (
        "/api/v1/instances",
        "/api/v1/instances/prod-rds",
        "/api/v1/databases",
        "/api/v1/users",
        "/api/v1/users/team-a/orders-api",
        "/api/v1/overview",
    ):
        body = client.get(path).text.lower()
        assert '"password":' not in body
        assert '"uri":' not in body


def test_overview_endpoint(client: TestClient) -> None:
    body = client.get("/api/v1/overview").json()
    assert body["instances"]["total"] == 2
    assert body["healthy"] is False
    assert isinstance(body["attention"], list)


def test_openapi_schema_is_served(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    assert "/api/v1/overview" in schema["paths"]
    assert schema["info"]["title"] == "pg-operator state API"


def test_phase_message_explains_a_drifted_resource() -> None:
    """A Drifted resource must not be described by its Ready message.

    Ready says "in sync" because the pass completed; the reason it is Drifted
    lives on the Drifted condition, and that is what an operator needs to see.
    """
    from pg_operator.api.schemas import phase_message

    status = {
        "conditions": [
            {"type": "Ready", "status": "True", "message": "orders in sync"},
            {
                "type": "Drifted",
                "status": "True",
                "reason": "ParameterDenied",
                "message": "parameter log_min_duration_statement could not be set",
            },
        ]
    }
    assert phase_message(status) == "parameter log_min_duration_statement could not be set"


def test_phase_message_prefers_ready_when_not_ready() -> None:
    status = {
        "conditions": [
            {"type": "Ready", "status": "False", "message": "cannot connect"},
            {"type": "Drifted", "status": "True", "message": "stale"},
        ]
    }
    from pg_operator.api.schemas import phase_message

    assert phase_message(status) == "cannot connect"


def test_phase_message_falls_back_to_ready_when_in_sync() -> None:
    from pg_operator.api.schemas import phase_message

    status = {
        "conditions": [
            {"type": "Ready", "status": "True", "message": "orders in sync"},
            {"type": "Drifted", "status": "False", "message": "live state matches spec"},
        ]
    }
    assert phase_message(status) == "orders in sync"
