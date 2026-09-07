"""Environment-driven operator settings."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _env_list(name: str) -> list[str]:
    raw = _env(name)
    if not raw:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


@dataclass(frozen=True, slots=True)
class Settings:
    """Runtime configuration for both the operator and the API container."""

    # --- scoping -----------------------------------------------------------
    #: Namespaces to watch for PostgresDB / PostgresUser. Empty means cluster-wide.
    watch_namespaces: list[str] = field(default_factory=list)
    #: Namespace the operator itself runs in; default target for created objects.
    operator_namespace: str = "pg-operator"

    # --- reconciliation ----------------------------------------------------
    #: Periodic full-reconcile interval, in seconds. Drives drift correction.
    reconcile_interval: int = 300
    #: Idle delay before a timer re-fires after a change, in seconds.
    reconcile_idle: int = 30
    #: Backoff applied when a handler raises a temporary error, in seconds.
    retry_backoff: int = 30
    #: Hard ceiling on a single reconcile pass, in seconds.
    reconcile_timeout: int = 300

    # --- postgres ----------------------------------------------------------
    connect_timeout: int = 10
    statement_timeout_ms: int = 30_000
    pool_max_size: int = 4
    pool_max_idle: int = 120
    application_name: str = "pg-operator"

    # --- credentials -------------------------------------------------------
    default_password_length: int = 32

    # --- push secrets ------------------------------------------------------
    pushsecret_enabled: bool = True
    pushsecret_api_version: str = "external-secrets.io/v1"

    # --- observability -----------------------------------------------------
    log_level: str = "INFO"
    log_format: str = "json"
    metrics_enabled: bool = True
    metrics_port: int = 9090

    # --- api ---------------------------------------------------------------
    api_host: str = "0.0.0.0"  # noqa: S104 - container listens on all interfaces
    api_port: int = 8000
    api_root_path: str = ""
    api_cors_origins: list[str] = field(default_factory=list)
    api_cache_ttl: int = 5

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            watch_namespaces=_env_list("PG_OPERATOR_WATCH_NAMESPACES"),
            operator_namespace=_env("PG_OPERATOR_NAMESPACE", "pg-operator"),
            reconcile_interval=_env_int("PG_OPERATOR_RECONCILE_INTERVAL", 300),
            reconcile_idle=_env_int("PG_OPERATOR_RECONCILE_IDLE", 30),
            retry_backoff=_env_int("PG_OPERATOR_RETRY_BACKOFF", 30),
            reconcile_timeout=_env_int("PG_OPERATOR_RECONCILE_TIMEOUT", 300),
            connect_timeout=_env_int("PG_OPERATOR_CONNECT_TIMEOUT", 10),
            statement_timeout_ms=_env_int("PG_OPERATOR_STATEMENT_TIMEOUT_MS", 30_000),
            pool_max_size=_env_int("PG_OPERATOR_POOL_MAX_SIZE", 4),
            pool_max_idle=_env_int("PG_OPERATOR_POOL_MAX_IDLE", 120),
            application_name=_env("PG_OPERATOR_APPLICATION_NAME", "pg-operator"),
            default_password_length=_env_int("PG_OPERATOR_PASSWORD_LENGTH", 32),
            pushsecret_enabled=_env_bool("PG_OPERATOR_PUSHSECRET_ENABLED", True),
            pushsecret_api_version=_env(
                "PG_OPERATOR_PUSHSECRET_API_VERSION", "external-secrets.io/v1"
            ),
            log_level=_env("PG_OPERATOR_LOG_LEVEL", "INFO").upper(),
            log_format=_env("PG_OPERATOR_LOG_FORMAT", "json").lower(),
            metrics_enabled=_env_bool("PG_OPERATOR_METRICS_ENABLED", True),
            metrics_port=_env_int("PG_OPERATOR_METRICS_PORT", 9090),
            api_host=_env("PG_OPERATOR_API_HOST", "0.0.0.0"),  # noqa: S104
            api_port=_env_int("PG_OPERATOR_API_PORT", 8000),
            api_root_path=_env("PG_OPERATOR_API_ROOT_PATH", ""),
            api_cors_origins=_env_list("PG_OPERATOR_API_CORS_ORIGINS"),
            api_cache_ttl=_env_int("PG_OPERATOR_API_CACHE_TTL", 5),
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings, resolved once from the environment."""
    return Settings.from_env()
