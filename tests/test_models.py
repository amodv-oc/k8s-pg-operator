"""Spec parsing: defaults, derivations and the validations CRDs cannot express."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from pg_operator.models import (
    effective_retention,
    parse_database_spec,
    parse_instance_spec,
    parse_user_spec,
)

MINIMAL_INSTANCE = {
    "host": "prod.rds.amazonaws.com",
    "credentialsSecretRef": {"name": "creds", "namespace": "pg-operator"},
}


# --- PostgresInstance ------------------------------------------------------


def test_instance_defaults() -> None:
    spec = parse_instance_spec(MINIMAL_INSTANCE)
    assert spec.port == 5432
    assert spec.maintenance_database == "postgres"
    assert spec.ssl_mode == "require"
    assert spec.retention_policy == "RETAIN"
    assert spec.credentials_secret_ref.username_key == "username"


def test_instance_credentials_namespace_falls_back_to_operator_namespace() -> None:
    spec = parse_instance_spec({**MINIMAL_INSTANCE, "credentialsSecretRef": {"name": "c"}})
    assert spec.credentials_secret_ref.resolve_namespace("pg-operator") == "pg-operator"


def test_verify_modes_require_a_ca_bundle() -> None:
    with pytest.raises(ValidationError, match="sslRootCertSecretRef"):
        parse_instance_spec({**MINIMAL_INSTANCE, "sslMode": "verify-full"})

    spec = parse_instance_spec(
        {
            **MINIMAL_INSTANCE,
            "sslMode": "verify-full",
            "sslRootCertSecretRef": {"name": "ca", "key": "ca.pem"},
        }
    )
    assert spec.ssl_root_cert_secret_ref is not None


def test_allowed_namespaces_gate() -> None:
    spec = parse_instance_spec({**MINIMAL_INSTANCE, "allowedNamespaces": ["team-a"]})
    assert spec.permits_namespace("team-a")
    assert not spec.permits_namespace("team-b")
    # Empty means unrestricted.
    assert parse_instance_spec(MINIMAL_INSTANCE).permits_namespace("anything")


def test_unknown_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        parse_instance_spec({**MINIMAL_INSTANCE, "hostname": "typo"})


def test_push_secret_requires_a_store_when_enabled() -> None:
    with pytest.raises(ValidationError, match="secretStoreRefs"):
        parse_instance_spec({**MINIMAL_INSTANCE, "pushSecret": {"enabled": True}})


# --- PostgresDB ------------------------------------------------------------


def test_database_name_defaults_to_a_sanitised_resource_name() -> None:
    spec = parse_database_spec({"instanceRef": {"name": "prod"}})
    assert spec.resolve_database_name("orders-api") == "orders_api"


def test_explicit_database_name_is_used_verbatim() -> None:
    spec = parse_database_spec(
        {"instanceRef": {"name": "prod"}, "databaseName": "orders_prod"}
    )
    assert spec.resolve_database_name("ignored") == "orders_prod"


def test_public_schema_is_always_managed() -> None:
    spec = parse_database_spec(
        {"instanceRef": {"name": "prod"}, "schemas": [{"name": "audit"}]}
    )
    # public exists in every database from template0/1; not managing it would
    # silently deny the group roles access to it.
    assert [s.name for s in spec.resolve_schemas()] == ["public", "audit"]


def test_declared_public_schema_is_not_duplicated() -> None:
    spec = parse_database_spec(
        {
            "instanceRef": {"name": "prod"},
            "schemas": [{"name": "public", "comment": "default"}, {"name": "audit"}],
        }
    )
    names = [s.name for s in spec.resolve_schemas()]
    assert names == ["public", "audit"]
    assert spec.resolve_schemas()[0].comment == "default"


def test_role_names_derive_from_the_database_name() -> None:
    spec = parse_database_spec({"instanceRef": {"name": "prod"}})
    assert spec.resolve_role_names("orders") == {
        "owner": "orders_owner",
        "readWrite": "orders_rw",
        "readOnly": "orders_ro",
    }


def test_role_name_overrides_are_honoured() -> None:
    spec = parse_database_spec(
        {
            "instanceRef": {"name": "prod"},
            "roles": {"owner": "app_admin", "readOnly": "app_reader"},
        }
    )
    resolved = spec.resolve_role_names("orders")
    assert resolved["owner"] == "app_admin"
    assert resolved["readOnly"] == "app_reader"
    # Unset overrides keep the derived default.
    assert resolved["readWrite"] == "orders_rw"


def test_colliding_role_overrides_are_rejected() -> None:
    with pytest.raises(ValidationError, match="distinct"):
        parse_database_spec(
            {
                "instanceRef": {"name": "prod"},
                "roles": {"owner": "same", "readWrite": "same"},
            }
        )


def test_duplicate_schemas_and_extensions_are_rejected() -> None:
    with pytest.raises(ValidationError, match="duplicate"):
        parse_database_spec(
            {"instanceRef": {"name": "prod"}, "schemas": [{"name": "a"}, {"name": "a"}]}
        )
    with pytest.raises(ValidationError, match="duplicate"):
        parse_database_spec(
            {
                "instanceRef": {"name": "prod"},
                "extensions": [{"name": "citext"}, {"name": "citext"}],
            }
        )


def test_illegal_schema_name_is_rejected() -> None:
    with pytest.raises(ValidationError):
        parse_database_spec(
            {"instanceRef": {"name": "prod"}, "schemas": [{"name": "Bad-Name"}]}
        )


def test_database_flag_defaults_are_the_safe_ones() -> None:
    spec = parse_database_spec({"instanceRef": {"name": "prod"}})
    assert spec.revoke_public_connect is True
    assert spec.revoke_public_schema_create is True
    assert spec.set_role_for_owners is True
    assert spec.connection_limit == -1


# --- PostgresUser ----------------------------------------------------------


def test_username_defaults_to_a_sanitised_resource_name() -> None:
    spec = parse_user_spec({"instanceRef": {"name": "prod"}})
    assert spec.resolve_username("orders-api") == "orders_api"


def test_reserved_prefix_is_rejected_for_usernames() -> None:
    with pytest.raises(ValidationError):
        parse_user_spec({"instanceRef": {"name": "prod"}, "username": "pg_evil"})


def test_access_maps_levels_to_group_role_keys() -> None:
    spec = parse_user_spec(
        {
            "instanceRef": {"name": "prod"},
            "access": [
                {"dbRef": {"name": "orders"}, "role": "OWNER"},
                {"dbRef": {"name": "billing"}, "role": "RW"},
                {"dbRef": {"name": "reporting"}, "role": "RO"},
            ],
        }
    )
    assert [entry.role_key for entry in spec.access] == ["owner", "readWrite", "readOnly"]


def test_a_user_cannot_hold_two_levels_on_one_database() -> None:
    with pytest.raises(ValidationError, match="more than once"):
        parse_user_spec(
            {
                "instanceRef": {"name": "prod"},
                "access": [
                    {"dbRef": {"name": "orders"}, "role": "RW"},
                    {"dbRef": {"name": "orders"}, "role": "RO"},
                ],
            }
        )


def test_same_name_in_different_namespaces_is_allowed() -> None:
    spec = parse_user_spec(
        {
            "instanceRef": {"name": "prod"},
            "access": [
                {"dbRef": {"name": "orders", "namespace": "team-a"}, "role": "RW"},
                {"dbRef": {"name": "orders", "namespace": "team-b"}, "role": "RO"},
            ],
        }
    )
    assert len(spec.access) == 2


def test_secret_name_and_key_defaults() -> None:
    spec = parse_user_spec({"instanceRef": {"name": "prod"}})
    assert spec.resolve_secret_name("orders-api") == "orders-api-pg-credentials"
    assert spec.resolve_secret_namespace("team-a") == "team-a"
    assert spec.secret_key("password") == "password"


def test_secret_keys_can_be_renamed() -> None:
    spec = parse_user_spec(
        {
            "instanceRef": {"name": "prod"},
            "generatedSecret": {"keys": {"password": "PGPASSWORD"}},
        }
    )
    assert spec.secret_key("password") == "PGPASSWORD"
    assert spec.secret_key("username") == "username"


def test_role_attributes_default_to_least_privilege() -> None:
    spec = parse_user_spec({"instanceRef": {"name": "prod"}})
    assert spec.attributes.as_mapping() == {
        "createDB": False,
        "createRole": False,
        "replication": False,
        "bypassRLS": False,
        # INHERIT must stay on, or group membership conveys nothing.
        "inherit": True,
    }


def test_role_attributes_can_be_requested() -> None:
    spec = parse_user_spec(
        {"instanceRef": {"name": "prod"}, "attributes": {"createDB": True}}
    )
    assert spec.attributes.as_mapping()["createDB"] is True


def test_password_length_bounds_are_enforced() -> None:
    with pytest.raises(ValidationError):
        parse_user_spec({"instanceRef": {"name": "prod"}, "passwordLength": 8})


# --- retention -------------------------------------------------------------


@pytest.mark.parametrize(
    ("child", "instance_default", "expected"),
    [
        (None, "RETAIN", "RETAIN"),
        (None, "DROP", "DROP"),
        ("DROP", "RETAIN", "DROP"),
        ("RETAIN", "DROP", "RETAIN"),
    ],
)
def test_retention_inherits_from_the_instance_unless_set(
    child: str | None, instance_default: str, expected: str
) -> None:
    assert effective_retention(child, instance_default) == expected  # type: ignore[arg-type]
