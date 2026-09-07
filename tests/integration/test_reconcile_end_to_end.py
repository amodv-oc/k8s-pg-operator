"""The real reconcilers, driven against a live server with a faked cluster.

These exercise ``reconcile_database`` / ``reconcile_user`` / the teardown paths
as the kopf handlers call them, so they cover the ordering, status writing and
retention decisions rather than the SQL primitives alone.
"""

from __future__ import annotations

from typing import Any

import pytest
from psycopg import sql

from pg_operator.config import Settings
from pg_operator.constants import PLURAL_DATABASE, PLURAL_INSTANCE, PLURAL_USER
from pg_operator.postgres import database as pgdb
from pg_operator.postgres import roles as pgroles
from pg_operator.postgres.connection import Endpoint, PoolRegistry
from pg_operator.postgres.sql import execute, fetch_scalar
from pg_operator.reconcile.context import ReconcileContext
from pg_operator.reconcile.database import (
    build_status as build_db_status,
)
from pg_operator.reconcile.database import (
    reconcile_database,
    teardown_database,
)
from pg_operator.reconcile.instance import reconcile_instance
from pg_operator.reconcile.user import (
    build_status as build_user_status,
)
from pg_operator.reconcile.user import (
    reconcile_user,
    teardown_user,
)

from ..conftest import drop_database_hard, drop_roles_hard, requires_postgres
from ..fakes import FakeK8sClient

pytestmark = [requires_postgres, pytest.mark.asyncio]

NS = "team-a"
OPERATOR_NS = "pg-operator"
INSTANCE = "test-rds"


@pytest.fixture
def k8s(endpoint: Endpoint) -> FakeK8sClient:
    """A cluster holding the instance, its credentials and the PushSecret CRD."""
    client = FakeK8sClient(crds={"pushsecrets.external-secrets.io": ["v1"]})
    client.core.seed(
        OPERATOR_NS,
        "rds-superuser",
        {"username": endpoint.username, "password": endpoint.password},
    )
    client.custom.add(
        {
            "apiVersion": "postgres.opsplatform.io/v1alpha1",
            "kind": "PostgresInstance",
            "metadata": {"name": INSTANCE, "generation": 1},
            "spec": {
                "host": endpoint.host,
                "port": endpoint.port,
                "maintenanceDatabase": endpoint.maintenance_database,
                "sslMode": endpoint.ssl_mode,
                "credentialsSecretRef": {
                    "name": "rds-superuser",
                    "namespace": OPERATOR_NS,
                },
            },
        },
        PLURAL_INSTANCE,
    )
    return client


@pytest.fixture
async def ctx(k8s: FakeK8sClient, settings: Settings):
    context = ReconcileContext(k8s, settings=Settings(operator_namespace=OPERATOR_NS))
    try:
        yield context
    finally:
        await context.close()


def db_resource(name: str, **spec: Any) -> dict[str, Any]:
    return {
        "apiVersion": "postgres.opsplatform.io/v1alpha1",
        "kind": "PostgresDB",
        "metadata": {"name": name, "namespace": NS, "generation": 1, "uid": "db-uid"},
        "spec": {"instanceRef": {"name": INSTANCE}, **spec},
    }


def user_resource(name: str, **spec: Any) -> dict[str, Any]:
    return {
        "apiVersion": "postgres.opsplatform.io/v1alpha1",
        "kind": "PostgresUser",
        "metadata": {"name": name, "namespace": NS, "generation": 1, "uid": "user-uid"},
        "spec": {"instanceRef": {"name": INSTANCE}, **spec},
    }


async def run_db(ctx: ReconcileContext, obj: dict[str, Any]) -> dict[str, Any]:
    """Reconcile a PostgresDB and write status back, as the handler does."""
    result, instance = await reconcile_database(ctx, obj)
    obj["status"] = build_db_status(result, instance, obj.get("status"), 1)
    return obj["status"]


async def run_user(ctx: ReconcileContext, obj: dict[str, Any]) -> dict[str, Any]:
    result, instance = await reconcile_user(ctx, obj)
    obj["status"] = build_user_status(result, instance, obj.get("status"), 1)
    return obj["status"]


@pytest.fixture
async def cleanup(ctx: ReconcileContext, pools: PoolRegistry, endpoint: Endpoint):
    """Drop whatever a test created, whatever the retention policy said.

    Depends on ``ctx`` so it tears down first, and closes the reconciler's own
    pools: a pool the operator still holds counts as an open connection and
    blocks DROP DATABASE.
    """
    databases: list[str] = []
    roles: list[str] = []
    yield databases, roles
    for database in databases:
        # The reconciler holds its own pools; both must be released before the
        # drop, or they count as open connections.
        await ctx.pools.close_database(endpoint.instance, database)
        await drop_database_hard(pools, endpoint, database)
    await drop_roles_hard(pools, endpoint, roles)


# --- instance --------------------------------------------------------------


async def test_instance_reconcile_reports_server_capabilities(ctx, k8s) -> None:
    obj = await k8s.custom.get_cluster_custom_object(
        group="", version="", plural=PLURAL_INSTANCE, name=INSTANCE
    )
    result = await reconcile_instance(ctx, obj)
    assert result.reachable is True
    assert int(result.status["serverVersion"].split(".")[0]) >= 14
    assert "CREATEDB" in result.status["privileges"]
    assert result.status["pushSecretsAvailable"] is True


async def test_instance_reports_a_missing_pushsecret_crd(ctx, k8s) -> None:
    k8s.apiextensions.crds = {}
    obj = await k8s.custom.get_cluster_custom_object(
        group="", version="", plural=PLURAL_INSTANCE, name=INSTANCE
    )
    result = await reconcile_instance(ctx, obj)
    assert result.status["pushSecretsAvailable"] is False
    synced = next(c for c in result.conditions if c["type"] == "Synced")
    assert synced["status"] == "False"
    assert "PushSecret CRD is not installed" in synced["message"]


async def test_unreachable_instance_is_reported_not_raised(ctx, k8s) -> None:
    obj = await k8s.custom.get_cluster_custom_object(
        group="", version="", plural=PLURAL_INSTANCE, name=INSTANCE
    )
    obj["spec"]["port"] = 1  # nothing listening
    result = await reconcile_instance(ctx, obj)
    assert result.reachable is False
    assert result.status["phase"] == "Unreachable"


# --- database --------------------------------------------------------------


async def test_database_reconcile_creates_everything(ctx, cleanup, unique) -> None:
    databases, roles = cleanup
    name = f"e2e_db_{unique}"
    databases.append(name)
    roles.extend([f"{name}_owner", f"{name}_rw", f"{name}_ro"])

    obj = db_resource(name, schemas=[{"name": "audit"}])
    status = await run_db(ctx, obj)

    instance = await ctx.resolve_instance(INSTANCE, for_namespace=NS)
    if instance.server.public_schema_is_locked_down:
        assert status["phase"] == "Ready"
        assert status["unownedSchemas"] == []
    else:
        # Before PostgreSQL 15 `public` belongs to the bootstrap superuser and
        # the managing role cannot take it, which must be reported, not hidden.
        assert status["phase"] == "Drifted"
        assert status["unownedSchemas"] == ["public"]
        synced = next(c for c in status["conditions"] if c["type"] == "Synced")
        assert synced["reason"] == "SchemaNotOwned"
        assert "ALTER SCHEMA public OWNER TO" in synced["message"]
    assert status["databaseName"] == name
    assert status["retentionPolicy"] == "RETAIN"
    assert status["roles"] == {
        "owner": f"{name}_owner",
        "readWrite": f"{name}_rw",
        "readOnly": f"{name}_ro",
    }
    assert status["managedSchemas"] == ["audit", "public"]
    assert status["orphanedSchemas"] == []
    assert status["schemaCount"] == 2
    # Asserted on `audit`: whether `public` is manageable is version-dependent.
    grants = status["schemaGrants"]
    assert f"{name}_owner:USAGE+CREATE" in grants["audit"]
    assert f"{name}_rw:USAGE" in grants["audit"]
    assert f"{name}_ro:USAGE" in grants["audit"]


async def test_second_reconcile_reports_no_changes(ctx, cleanup, unique) -> None:
    """Steady state must be a genuine no-op, or status churns forever."""
    databases, roles = cleanup
    name = f"e2e_idem_{unique}"
    databases.append(name)
    roles.extend([f"{name}_owner", f"{name}_rw", f"{name}_ro"])

    obj = db_resource(
        name, schemas=[{"name": "audit"}], parameters={"search_path": "public, audit"}
    )
    await run_db(ctx, obj)
    result, _ = await reconcile_database(ctx, obj)

    assert result.created is False
    assert result.changes == [], f"unexpected changes on a steady-state pass: {result.changes}"
    assert result.message.endswith("in sync")


async def test_adding_a_schema_later_is_applied(ctx, cleanup, unique, pools, endpoint) -> None:
    """The requirement: a schema added to an existing PostgresDB is created."""
    databases, roles = cleanup
    name = f"e2e_add_{unique}"
    databases.append(name)
    roles.extend([f"{name}_owner", f"{name}_rw", f"{name}_ro"])

    obj = db_resource(name)
    first = await run_db(ctx, obj)
    assert first["managedSchemas"] == ["public"]

    # A later spec change, exactly as a `kubectl apply` would produce.
    obj["spec"]["schemas"] = [{"name": "audit"}, {"name": "reporting"}]
    obj["metadata"]["generation"] = 2
    result, instance = await reconcile_database(ctx, obj)
    obj["status"] = build_db_status(result, instance, obj["status"], 2)

    assert "created schema audit" in result.changes
    assert "created schema reporting" in result.changes
    assert obj["status"]["managedSchemas"] == ["audit", "public", "reporting"]

    # And the new schemas are immediately usable by the pre-existing roles.
    async with pools.connection(endpoint, name) as conn:
        await execute(conn, sql.SQL("SET ROLE {}").format(sql.Identifier(f"{name}_owner")))
        await execute(conn, sql.SQL("CREATE TABLE reporting.metrics (id int)"))
        await execute(conn, sql.SQL("RESET ROLE"))
        ro_select = await fetch_scalar(
            conn,
            sql.SQL("SELECT has_table_privilege(%s, 'reporting.metrics', 'SELECT')"),
            (f"{name}_ro",),
        )
        rw_insert = await fetch_scalar(
            conn,
            sql.SQL("SELECT has_table_privilege(%s, 'reporting.metrics', 'INSERT')"),
            (f"{name}_rw",),
        )
    assert ro_select is True
    assert rw_insert is True


async def test_removed_schema_is_retained_and_reported(ctx, cleanup, unique) -> None:
    databases, roles = cleanup
    name = f"e2e_keep_{unique}"
    databases.append(name)
    roles.extend([f"{name}_owner", f"{name}_rw", f"{name}_ro"])

    obj = db_resource(name, schemas=[{"name": "audit"}])
    await run_db(ctx, obj)

    obj["spec"]["schemas"] = []
    obj["metadata"]["generation"] = 2
    result, instance = await reconcile_database(ctx, obj)
    status = build_db_status(result, instance, obj["status"], 2)

    assert result.orphaned_schemas == ["audit"]
    assert status["phase"] == "Drifted"
    drift = next(c for c in status["conditions"] if c["type"] == "Drifted")
    assert "audit" in drift["message"]
    assert "RETAIN so nothing was dropped" in drift["message"]
    # Retained means retained: the schema is still there.
    assert "audit" in status["observedSchemas"]


async def test_a_retained_orphan_keeps_being_reported(ctx, cleanup, unique) -> None:
    """A retained orphan must not fade off the status after one pass.

    ``managedSchemas`` is the record of what the operator created; orphan
    detection compares the spec against it. Rewriting it from the spec alone
    would drop the retained orphan from the record, so the second pass would see
    nothing out of place: the Drifted condition would silently clear and the
    schema would be reclassified as someone else's object - at which point
    switching to DROP could no longer remove it.
    """
    databases, roles = cleanup
    name = f"e2e_stick_{unique}"
    databases.append(name)
    roles.extend([f"{name}_owner", f"{name}_rw", f"{name}_ro"])

    obj = db_resource(name, schemas=[{"name": "audit"}])
    await run_db(ctx, obj)

    obj["spec"]["schemas"] = []
    obj["metadata"]["generation"] = 2
    first = await run_db(ctx, obj)
    assert first["orphanedSchemas"] == ["audit"]

    # Second pass, spec unchanged: the orphan is still an orphan.
    second = await run_db(ctx, obj)
    assert second["orphanedSchemas"] == ["audit"]
    assert second["phase"] == "Drifted"
    assert "audit" in second["managedSchemas"]
    assert second["unmanagedSchemas"] == []

    # And it is still eligible for dropping when the policy changes.
    obj["spec"]["retentionPolicy"] = "DROP"
    obj["metadata"]["generation"] = 3
    third = await run_db(ctx, obj)
    assert third["orphanedSchemas"] == []
    assert "audit" not in third["observedSchemas"]
    assert "audit" not in third["managedSchemas"]
    # No orphan drift remains. The phase itself is not asserted: before
    # PostgreSQL 15 this database is Drifted anyway, because `public` belongs to
    # the bootstrap superuser and its ownership cannot be taken.
    drift = next(c for c in third["conditions"] if c["type"] == "Drifted")
    assert drift["reason"] != "RetainedOrphans"
    assert "audit" not in drift["message"]


async def test_removed_schema_is_dropped_under_drop_policy(ctx, cleanup, unique) -> None:
    databases, roles = cleanup
    name = f"e2e_drops_{unique}"
    databases.append(name)
    roles.extend([f"{name}_owner", f"{name}_rw", f"{name}_ro"])

    obj = db_resource(name, retentionPolicy="DROP", schemas=[{"name": "audit"}])
    await run_db(ctx, obj)

    obj["spec"]["schemas"] = []
    obj["metadata"]["generation"] = 2
    result, instance = await reconcile_database(ctx, obj)
    status = build_db_status(result, instance, obj["status"], 2)

    assert any(
        change.startswith("dropped orphaned schema audit") for change in result.changes
    )
    assert status["orphanedSchemas"] == []
    assert "audit" not in status["observedSchemas"]
    # Ready unless this server also reports the unmanageable `public` schema.
    assert status["phase"] in {"Ready", "Drifted"}


async def test_schema_created_outside_the_operator_is_never_dropped(
    ctx, cleanup, unique, pools, endpoint
) -> None:
    """A DBA's schema is reported as unmanaged, even under a DROP policy."""
    databases, roles = cleanup
    name = f"e2e_unmanaged_{unique}"
    databases.append(name)
    roles.extend([f"{name}_owner", f"{name}_rw", f"{name}_ro"])

    obj = db_resource(name, retentionPolicy="DROP")
    await run_db(ctx, obj)

    async with pools.connection(endpoint, name) as conn:
        await execute(conn, sql.SQL("CREATE SCHEMA handmade"))

    obj["metadata"]["generation"] = 2
    result, instance = await reconcile_database(ctx, obj)
    status = build_db_status(result, instance, obj["status"], 2)

    assert status["unmanagedSchemas"] == ["handmade"]
    assert status["orphanedSchemas"] == []
    assert "handmade" in status["observedSchemas"], "an unmanaged schema must survive"


async def test_immutable_encoding_change_is_reported_not_attempted(
    ctx, cleanup, unique
) -> None:
    databases, roles = cleanup
    name = f"e2e_enc_{unique}"
    databases.append(name)
    roles.extend([f"{name}_owner", f"{name}_rw", f"{name}_ro"])

    obj = db_resource(name)
    await run_db(ctx, obj)

    obj["spec"]["encoding"] = "LATIN1"
    obj["metadata"]["generation"] = 2
    result, instance = await reconcile_database(ctx, obj)
    status = build_db_status(result, instance, obj["status"], 2)

    assert any("encoding is UTF8" in msg for msg in result.immutable_drift)
    synced = next(c for c in status["conditions"] if c["type"] == "Synced")
    assert synced["status"] == "False"
    # On PostgreSQL < 15 the unmanageable `public` schema is also reported, and
    # the reason becomes MultipleIssues rather than hiding one of them.
    assert synced["reason"] in {"ImmutableFieldDrift", "MultipleIssues"}
    assert "encoding is UTF8" in synced["message"]


async def test_encoding_spelling_variants_are_not_drift(ctx, cleanup, unique) -> None:
    databases, roles = cleanup
    name = f"e2e_enc2_{unique}"
    databases.append(name)
    roles.extend([f"{name}_owner", f"{name}_rw", f"{name}_ro"])

    obj = db_resource(name, encoding="UTF-8")
    await run_db(ctx, obj)
    result, _ = await reconcile_database(ctx, obj)
    assert result.immutable_drift == []


# --- user ------------------------------------------------------------------


@pytest.fixture
async def provisioned_db(ctx, k8s, cleanup, unique):
    """A reconciled PostgresDB, registered in the fake cluster."""
    databases, roles = cleanup
    name = f"e2e_u_{unique}"
    databases.append(name)
    roles.extend([f"{name}_owner", f"{name}_rw", f"{name}_ro"])

    obj = db_resource(name, schemas=[{"name": "audit"}])
    k8s.custom.add(obj, PLURAL_DATABASE)
    await run_db(ctx, obj)
    return name, obj, roles


async def test_user_reconcile_creates_role_grant_and_secret(
    ctx, k8s, provisioned_db, cleanup
) -> None:
    database, db_obj, _roles = provisioned_db
    _, tracked_roles = cleanup
    obj = user_resource(
        "orders-api", access=[{"dbRef": {"name": db_obj["metadata"]["name"]}, "role": "RW"}]
    )
    k8s.custom.add(obj, PLURAL_USER)
    tracked_roles.append("orders_api")

    status = await run_user(ctx, obj)

    assert status["phase"] == "Ready"
    assert status["username"] == "orders_api"
    assert status["grantCount"] == 1
    assert status["grants"][0] == {
        "database": database,
        "role": "RW",
        "groupRole": f"{database}_rw",
        "dbRef": f"{NS}/{db_obj['metadata']['name']}",
    }

    secret = k8s.core.get(NS, "orders-api-pg-credentials")
    assert secret is not None
    assert secret["username"] == "orders_api"
    assert secret["database"] == database
    assert len(secret["password"]) == 32
    assert secret["uri"].startswith("postgresql://orders_api:")
    assert status["secretRef"] == {"name": "orders-api-pg-credentials", "namespace": NS}


async def test_generated_credential_actually_connects(
    ctx, k8s, provisioned_db, cleanup, endpoint, settings
) -> None:
    """The credential published to the Secret is the one the server accepts."""
    database, db_obj, _ = provisioned_db
    _, tracked_roles = cleanup
    obj = user_resource(
        "conn-check", access=[{"dbRef": {"name": db_obj["metadata"]["name"]}, "role": "RW"}]
    )
    k8s.custom.add(obj, PLURAL_USER)
    tracked_roles.append("conn_check")
    await run_user(ctx, obj)

    secret = k8s.core.get(NS, "conn-check-pg-credentials")
    assert secret is not None

    as_user = Endpoint(
        instance="as-user",
        host=secret["host"],
        port=int(secret["port"]),
        username=secret["username"],
        password=secret["password"],
        maintenance_database=secret["database"],
        ssl_mode="disable",
    )
    registry = PoolRegistry(settings)
    try:
        async with registry.connection(as_user, database) as conn:
            who = await fetch_scalar(conn, sql.SQL("SELECT current_user"))
            db_name = await fetch_scalar(conn, sql.SQL("SELECT current_database()"))
    finally:
        await registry.close_all()
    assert who == "conn_check"
    assert db_name == database


async def test_user_reconcile_is_idempotent_and_does_not_rewrite_the_password(
    ctx, k8s, provisioned_db, cleanup
) -> None:
    _database, db_obj, _ = provisioned_db
    _, tracked_roles = cleanup
    obj = user_resource(
        "idem-user", access=[{"dbRef": {"name": db_obj["metadata"]["name"]}, "role": "RO"}]
    )
    k8s.custom.add(obj, PLURAL_USER)
    tracked_roles.append("idem_user")

    await run_user(ctx, obj)
    first = k8s.core.get(NS, "idem-user-pg-credentials")["password"]

    result, _ = await reconcile_user(ctx, obj)
    second = k8s.core.get(NS, "idem-user-pg-credentials")["password"]

    assert result.created is False
    assert result.password_written is False, "a steady-state pass must not touch credentials"
    assert result.changes == []
    assert first == second


async def test_deleted_secret_is_recreated_with_a_working_password(
    ctx, k8s, provisioned_db, cleanup, settings
) -> None:
    """Self-healing: the old password is unrecoverable, so a new one is applied."""
    database, db_obj, _ = provisioned_db
    _, tracked_roles = cleanup
    obj = user_resource(
        "heal-user", access=[{"dbRef": {"name": db_obj["metadata"]["name"]}, "role": "RW"}]
    )
    k8s.custom.add(obj, PLURAL_USER)
    tracked_roles.append("heal_user")
    await run_user(ctx, obj)
    original = k8s.core.get(NS, "heal-user-pg-credentials")["password"]

    await k8s.core.delete_namespaced_secret("heal-user-pg-credentials", NS)
    result, _ = await reconcile_user(ctx, obj)

    recreated = k8s.core.get(NS, "heal-user-pg-credentials")
    assert recreated is not None
    assert recreated["password"] != original
    assert result.password_written is True
    assert any("Secret was missing" in change for change in result.changes)

    # And the new credential works.
    as_user = Endpoint(
        instance="healed",
        host=recreated["host"],
        port=int(recreated["port"]),
        username=recreated["username"],
        password=recreated["password"],
        maintenance_database=recreated["database"],
        ssl_mode="disable",
    )
    registry = PoolRegistry(settings)
    try:
        async with registry.connection(as_user, database) as conn:
            assert await fetch_scalar(conn, sql.SQL("SELECT 1")) == 1
    finally:
        await registry.close_all()


async def test_byo_password_is_adopted(ctx, k8s, provisioned_db, cleanup, settings) -> None:
    database, db_obj, _ = provisioned_db
    _, tracked_roles = cleanup
    k8s.core.seed(NS, "byo-creds", {"password": "Supplied-Password-123"})
    obj = user_resource(
        "byo-user",
        access=[{"dbRef": {"name": db_obj["metadata"]["name"]}, "role": "RW"}],
        passwordSecretRef={"name": "byo-creds", "key": "password"},
    )
    k8s.custom.add(obj, PLURAL_USER)
    tracked_roles.append("byo_user")
    await run_user(ctx, obj)

    published = k8s.core.get(NS, "byo-user-pg-credentials")
    assert published["password"] == "Supplied-Password-123"

    as_user = Endpoint(
        instance="byo",
        host=published["host"],
        port=int(published["port"]),
        username="byo_user",
        password="Supplied-Password-123",
        maintenance_database=database,
        ssl_mode="disable",
    )
    registry = PoolRegistry(settings)
    try:
        async with registry.connection(as_user, database) as conn:
            assert await fetch_scalar(conn, sql.SQL("SELECT current_user")) == "byo_user"
    finally:
        await registry.close_all()


async def test_changing_access_level_swaps_the_membership(
    ctx, k8s, provisioned_db, cleanup, pools, endpoint
) -> None:
    database, db_obj, _ = provisioned_db
    _, tracked_roles = cleanup
    obj = user_resource(
        "level-user", access=[{"dbRef": {"name": db_obj["metadata"]["name"]}, "role": "RW"}]
    )
    k8s.custom.add(obj, PLURAL_USER)
    tracked_roles.append("level_user")
    await run_user(ctx, obj)

    obj["spec"]["access"] = [
        {"dbRef": {"name": db_obj["metadata"]["name"]}, "role": "RO"}
    ]
    obj["metadata"]["generation"] = 2
    result, _ = await reconcile_user(ctx, obj)

    assert f"granted {database}_ro" in result.changes
    assert f"revoked {database}_rw" in result.changes
    async with pools.connection(endpoint) as conn:
        held = await pgroles.memberships(conn, "level_user")
    assert f"{database}_ro" in held
    assert f"{database}_rw" not in held


async def test_owner_access_sets_the_per_database_role(
    ctx, k8s, provisioned_db, cleanup, pools, endpoint
) -> None:
    database, db_obj, _ = provisioned_db
    _, tracked_roles = cleanup
    obj = user_resource(
        "owner-user", access=[{"dbRef": {"name": db_obj["metadata"]["name"]}, "role": "OWNER"}]
    )
    k8s.custom.add(obj, PLURAL_USER)
    tracked_roles.append("owner_user")
    result, _ = await reconcile_user(ctx, obj)

    assert any("set default role" in change for change in result.changes)
    async with pools.connection(endpoint) as conn:
        params = await pgroles.get_role_parameters(conn, "owner_user", database)
    assert params.get("role") == f"{database}_owner"


async def test_removing_owner_access_clears_the_default_role(
    ctx, k8s, provisioned_db, cleanup, pools, endpoint
) -> None:
    database, db_obj, _ = provisioned_db
    _, tracked_roles = cleanup
    obj = user_resource(
        "demote-user", access=[{"dbRef": {"name": db_obj["metadata"]["name"]}, "role": "OWNER"}]
    )
    k8s.custom.add(obj, PLURAL_USER)
    tracked_roles.append("demote_user")
    await run_user(ctx, obj)

    obj["spec"]["access"] = [{"dbRef": {"name": db_obj["metadata"]["name"]}, "role": "RO"}]
    obj["metadata"]["generation"] = 2
    result, _ = await reconcile_user(ctx, obj)

    assert any("cleared default role" in change for change in result.changes)
    async with pools.connection(endpoint) as conn:
        params = await pgroles.get_role_parameters(conn, "demote_user", database)
    assert "role" not in params


async def test_push_secret_is_created_and_removed(ctx, k8s, provisioned_db, cleanup) -> None:
    _database, db_obj, _ = provisioned_db
    _, tracked_roles = cleanup
    obj = user_resource(
        "push-user",
        access=[{"dbRef": {"name": db_obj["metadata"]["name"]}, "role": "RW"}],
        pushSecret={
            "secretStoreRefs": [{"name": "aws-sm", "kind": "ClusterSecretStore"}],
            "remoteRefKey": "prod/{{ .namespace }}/{{ .name }}",
        },
    )
    k8s.custom.add(obj, PLURAL_USER)
    tracked_roles.append("push_user")
    status = await run_user(ctx, obj)

    assert status["pushSecretRef"] == {"name": "push-user-pg-push", "namespace": NS}
    push = k8s.custom.get_stored("pushsecrets", NS, "push-user-pg-push")
    assert push is not None
    assert push["apiVersion"] == "external-secrets.io/v1"
    assert push["spec"]["selector"]["secret"]["name"] == "push-user-pg-credentials"
    assert push["spec"]["deletionPolicy"] == "None"
    remote_keys = {
        entry["match"]["remoteRef"]["remoteKey"] for entry in push["spec"]["data"]
    }
    assert remote_keys == {f"prod/{NS}/push-user"}

    # Turning it off removes the PushSecret.
    del obj["spec"]["pushSecret"]
    obj["metadata"]["generation"] = 2
    result, _ = await reconcile_user(ctx, obj)
    assert any("deleted PushSecret" in change for change in result.changes)
    assert k8s.custom.get_stored("pushsecrets", NS, "push-user-pg-push") is None


async def test_grant_across_instances_is_a_configuration_error(
    ctx, k8s, provisioned_db, cleanup
) -> None:
    from pg_operator.errors import ConfigurationError

    _, db_obj, _ = provisioned_db
    db_obj["spec"]["instanceRef"] = {"name": "some-other-instance"}
    obj = user_resource(
        "cross-user", access=[{"dbRef": {"name": db_obj["metadata"]["name"]}, "role": "RW"}]
    )
    k8s.custom.add(obj, PLURAL_USER)
    with pytest.raises(ConfigurationError, match="cannot span instances"):
        await reconcile_user(ctx, obj)


async def test_namespace_gate_is_enforced(ctx, k8s, cleanup, unique) -> None:
    from pg_operator.errors import ConfigurationError

    instance = await k8s.custom.get_cluster_custom_object(
        group="", version="", plural=PLURAL_INSTANCE, name=INSTANCE
    )
    instance["spec"]["allowedNamespaces"] = ["other-team"]
    obj = db_resource(f"e2e_gate_{unique}")
    with pytest.raises(ConfigurationError, match="does not permit namespace"):
        await reconcile_database(ctx, obj)


# --- teardown --------------------------------------------------------------


async def test_user_teardown_retains_by_default(
    ctx, k8s, provisioned_db, cleanup, pools, endpoint
) -> None:
    _database, db_obj, _ = provisioned_db
    _, tracked_roles = cleanup
    obj = user_resource(
        "retain-user", access=[{"dbRef": {"name": db_obj["metadata"]["name"]}, "role": "RW"}]
    )
    k8s.custom.add(obj, PLURAL_USER)
    tracked_roles.append("retain_user")
    await run_user(ctx, obj)

    message = await teardown_user(ctx, obj)

    assert "retained role retain_user" in message
    async with pools.connection(endpoint) as conn:
        assert await pgroles.role_exists(conn, "retain_user") is True
    # The Secret must survive too, or the retained role becomes unusable.
    assert k8s.core.get(NS, "retain-user-pg-credentials") is not None


async def test_user_teardown_drops_everything_under_drop(
    ctx, k8s, provisioned_db, cleanup, pools, endpoint
) -> None:
    database, db_obj, _ = provisioned_db
    obj = user_resource(
        "drop-user",
        retentionPolicy="DROP",
        access=[{"dbRef": {"name": db_obj["metadata"]["name"]}, "role": "OWNER"}],
        pushSecret={"secretStoreRefs": [{"name": "aws-sm"}]},
    )
    k8s.custom.add(obj, PLURAL_USER)
    await run_user(ctx, obj)
    assert k8s.custom.get_stored("pushsecrets", NS, "drop-user-pg-push") is not None

    message = await teardown_user(ctx, obj)

    assert "dropped role drop_user" in message
    async with pools.connection(endpoint) as conn:
        assert await pgroles.role_exists(conn, "drop_user") is False
        assert await pgroles.get_role_parameters(conn, "drop_user", database) == {}
    assert k8s.core.get(NS, "drop-user-pg-credentials") is None
    assert k8s.custom.get_stored("pushsecrets", NS, "drop-user-pg-push") is None


async def test_owner_user_waits_when_its_database_is_absent(
    ctx, k8s, provisioned_db, cleanup, pools, endpoint
) -> None:
    """An absent database is a pending dependency, not a failure.

    A database can be missing at both ends of its life: before its PostgresDB
    has reconciled, and while it is being deleted with users still pointing at
    it. ``ALTER ROLE ... IN DATABASE`` fails outright in both cases, and failing
    the user reconcile over it produces a retry storm that nothing can clear.
    """
    database, db_obj, _ = provisioned_db
    obj = user_resource(
        "waiting-user",
        access=[{"dbRef": {"name": db_obj["metadata"]["name"]}, "role": "OWNER"}],
    )
    k8s.custom.add(obj, PLURAL_USER)
    await run_user(ctx, obj)

    # Take the database away while the PostgresUser still references it.
    db_obj["spec"]["retentionPolicy"] = "DROP"
    await teardown_database(ctx, db_obj)

    status = await run_user(ctx, obj)

    conditions = {c["type"]: c for c in status["conditions"]}
    assert conditions["Ready"]["reason"] == "PendingDependency"
    assert database in conditions["Ready"]["message"]
    # The role itself is still managed: only the per-database step waits.
    async with pools.connection(endpoint) as conn:
        assert await pgroles.role_exists(conn, "waiting_user") is True


async def test_user_teardown_survives_its_database_being_dropped_first(
    ctx, k8s, provisioned_db, cleanup, pools, endpoint
) -> None:
    """Deleting a PostgresDB and its PostgresUser together must not wedge.

    ``kubectl delete -f`` and namespace deletion both tear the two down
    concurrently, and the database usually loses. The user's cleanup then finds
    its database gone: it cannot connect to it, and ``ALTER ROLE ... IN DATABASE
    ... RESET role`` fails outright. If that is treated as an error the
    finalizer never clears, and the login role and its password stay live on the
    server for as long as the resource is stuck.
    """
    database, db_obj, _ = provisioned_db
    obj = user_resource(
        "orphan-user",
        retentionPolicy="DROP",
        access=[{"dbRef": {"name": db_obj["metadata"]["name"]}, "role": "OWNER"}],
    )
    k8s.custom.add(obj, PLURAL_USER)
    status = await run_user(ctx, obj)
    assert [g["database"] for g in status["grants"]] == [database]

    # The database goes first, exactly as the DROP-policy teardown would leave
    # things, while the user's status still records a grant on it.
    db_obj["spec"]["retentionPolicy"] = "DROP"
    await teardown_database(ctx, db_obj)
    async with pools.connection(endpoint) as conn:
        assert await pgdb.get_database(conn, database) is None

    message = await teardown_user(ctx, obj)

    assert "dropped role orphan_user" in message
    async with pools.connection(endpoint) as conn:
        assert await pgroles.role_exists(conn, "orphan_user") is False
    assert k8s.core.get(NS, "orphan-user-pg-credentials") is None
    # No pool may be left behind pointing at the database that went away.
    assert f"{endpoint.instance}/{database}" not in ctx.pools.open_pools


async def test_database_teardown_retains_by_default(ctx, cleanup, unique, pools, endpoint) -> None:
    databases, roles = cleanup
    name = f"e2e_tr_{unique}"
    databases.append(name)
    roles.extend([f"{name}_owner", f"{name}_rw", f"{name}_ro"])

    obj = db_resource(name)
    await run_db(ctx, obj)
    message = await teardown_database(ctx, obj)

    assert f"retained database {name}" in message
    async with pools.connection(endpoint) as conn:
        assert await pgdb.get_database(conn, name) is not None
        assert await pgroles.role_exists(conn, f"{name}_owner") is True


async def test_database_teardown_drops_under_drop(ctx, cleanup, unique, pools, endpoint) -> None:
    name = f"e2e_td_{unique}"
    obj = db_resource(name, retentionPolicy="DROP", schemas=[{"name": "audit"}])
    await run_db(ctx, obj)

    message = await teardown_database(ctx, obj)

    assert f"dropped database {name}" in message
    assert f"{name}_owner" in message
    async with pools.connection(endpoint) as conn:
        assert await pgdb.get_database(conn, name) is None
        for role in (f"{name}_owner", f"{name}_rw", f"{name}_ro"):
            assert await pgroles.role_exists(conn, role) is False


async def test_database_teardown_terminates_open_sessions(
    ctx, cleanup, unique, pools, endpoint, settings
) -> None:
    """An application holding a connection must not block a DROP policy."""
    name = f"e2e_tk_{unique}"
    obj = db_resource(name, retentionPolicy="DROP")
    await run_db(ctx, obj)

    holder = PoolRegistry(settings)
    try:
        async with holder.connection(endpoint, name) as conn:
            await fetch_scalar(conn, sql.SQL("SELECT 1"))
            message = await teardown_database(ctx, obj)
    finally:
        await holder.close_all()

    assert f"dropped database {name}" in message
    async with pools.connection(endpoint) as conn:
        assert await pgdb.get_database(conn, name) is None
