"""Translation between Kubernetes names and PostgreSQL identifiers.

Kubernetes names are RFC 1123 labels (lowercase alphanumerics and ``-``);
PostgreSQL unquoted identifiers allow ``[a-z_][a-z0-9_$]*`` and are truncated at
``NAMEDATALEN - 1`` (63) bytes. Every identifier the operator derives is
normalised here so the same input always yields the same object name, and so
nothing that reaches SQL can carry a quote or a NUL byte.
"""

from __future__ import annotations

import hashlib
import re

#: PostgreSQL truncates identifiers at NAMEDATALEN-1 bytes.
MAX_IDENTIFIER_LENGTH = 63

_INVALID_CHARS = re.compile(r"[^a-z0-9_]+")
_LEADING_INVALID = re.compile(r"^[^a-z_]+")

_RESERVED_ROLE_PREFIXES = ("pg_",)


class InvalidIdentifier(ValueError):
    """The supplied value cannot be used as a PostgreSQL identifier."""


def sanitize_identifier(value: str, *, kind: str = "identifier") -> str:
    """Derive a stable, lowercase PostgreSQL identifier from a Kubernetes name.

    ``my-app.db`` becomes ``my_app_db``. Values longer than 63 bytes are
    truncated and suffixed with a hash of the original so that two long names
    which share a prefix do not collide.
    """
    if not value or not value.strip():
        raise InvalidIdentifier(f"empty {kind}")

    candidate = _INVALID_CHARS.sub("_", value.strip().lower())
    candidate = _LEADING_INVALID.sub("", candidate)
    candidate = candidate.strip("_") or ""

    if not candidate:
        raise InvalidIdentifier(f"{kind} {value!r} contains no usable characters")

    if len(candidate.encode()) > MAX_IDENTIFIER_LENGTH:
        digest = hashlib.sha256(value.encode()).hexdigest()[:8]
        keep = MAX_IDENTIFIER_LENGTH - len(digest) - 1
        candidate = f"{_truncate_bytes(candidate, keep)}_{digest}"

    return candidate


def validate_identifier(value: str, *, kind: str = "identifier") -> str:
    """Validate an explicitly supplied identifier without rewriting it.

    Used for spec fields such as ``databaseName`` and ``username`` where the
    author chose the exact name: silently mangling it would be surprising, so an
    unusable value is an error instead.
    """
    if not value or not value.strip():
        raise InvalidIdentifier(f"empty {kind}")

    candidate = value.strip()
    if len(candidate.encode()) > MAX_IDENTIFIER_LENGTH:
        raise InvalidIdentifier(
            f"{kind} {candidate!r} exceeds {MAX_IDENTIFIER_LENGTH} bytes"
        )
    if not re.fullmatch(r"[a-z_][a-z0-9_$]*", candidate):
        raise InvalidIdentifier(
            f"{kind} {candidate!r} must match [a-z_][a-z0-9_$]* "
            "(lowercase, starting with a letter or underscore)"
        )
    return candidate


def validate_role_name(value: str, *, kind: str = "role") -> str:
    """Validate a role name, additionally rejecting the reserved ``pg_`` prefix."""
    candidate = validate_identifier(value, kind=kind)
    for prefix in _RESERVED_ROLE_PREFIXES:
        if candidate.startswith(prefix):
            raise InvalidIdentifier(
                f"{kind} {candidate!r} uses the reserved {prefix!r} prefix"
            )
    return candidate


def group_role_names(database: str, prefix: str = "") -> dict[str, str]:
    """Names of the three NOLOGIN group roles that back a database's access levels.

    ``prefix`` lets several logical environments share one instance without role
    name collisions (e.g. ``staging_`` -> ``staging_app_rw``).
    """
    base = f"{prefix}{database}" if prefix else database
    # Reserve room for the longest suffix so all three names truncate together.
    base = _truncate_for_suffix(base, len("_owner"))
    return {
        "owner": f"{base}_owner",
        "readWrite": f"{base}_rw",
        "readOnly": f"{base}_ro",
    }


def _truncate_for_suffix(value: str, suffix_length: int) -> str:
    keep = MAX_IDENTIFIER_LENGTH - suffix_length
    if len(value.encode()) <= keep:
        return value
    digest = hashlib.sha256(value.encode()).hexdigest()[:6]
    return f"{_truncate_bytes(value, keep - len(digest) - 1)}_{digest}"


def _truncate_bytes(value: str, limit: int) -> str:
    encoded = value.encode()
    if len(encoded) <= limit:
        return value
    return encoded[:limit].decode(errors="ignore").rstrip("_")


def secret_name(resource_name: str, suffix: str = "pg-credentials") -> str:
    """Default name of the Secret holding a PostgresUser's credentials."""
    name = f"{resource_name}-{suffix}"
    if len(name) <= 253:
        return name
    digest = hashlib.sha256(resource_name.encode()).hexdigest()[:8]
    return f"{resource_name[: 253 - len(suffix) - len(digest) - 2]}-{digest}-{suffix}"
