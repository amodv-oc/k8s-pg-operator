"""Password verification and setting parsing — the pure logic in the roles layer."""

from __future__ import annotations

import base64
import hashlib
import hmac

import pytest

from pg_operator.postgres.roles import _md5_matches, _scram_matches
from pg_operator.postgres.settings import parse_setconfig, unquote_setting


def build_scram_verifier(
    password: str, salt: bytes = b"0123456789abcdef", iterations: int = 4096
) -> str:
    """Build a verifier the way PostgreSQL stores one, for round-trip testing."""
    salted = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    stored_key = base64.b64encode(hashlib.sha256(client_key).digest()).decode()
    server_key = base64.b64encode(
        hmac.new(salted, b"Server Key", hashlib.sha256).digest()
    ).decode()
    return (
        f"SCRAM-SHA-256${iterations}:{base64.b64encode(salt).decode()}"
        f"${stored_key}:{server_key}"
    )


async def test_scram_accepts_the_correct_password() -> None:
    verifier = build_scram_verifier("correct-horse-battery")
    assert await _scram_matches(None, verifier, "correct-horse-battery") is True


async def test_scram_rejects_a_wrong_password() -> None:
    verifier = build_scram_verifier("correct-horse-battery")
    assert await _scram_matches(None, verifier, "wrong") is False


@pytest.mark.parametrize(
    "verifier",
    [
        "SCRAM-SHA-256$broken",
        "SCRAM-SHA-256$notanumber:c2FsdA==$a:b",
        "SCRAM-SHA-256$4096:!!!not-base64!!!$a:b",
        "",
    ],
)
async def test_scram_abstains_on_an_unparseable_verifier(verifier: str) -> None:
    """Abstaining (None) is what stops a reconcile rewriting a working password."""
    assert await _scram_matches(None, verifier, "anything") is None


async def test_md5_verifier_round_trip() -> None:
    username, password = "svc_api", "hunter2"
    digest = hashlib.md5((password + username).encode(), usedforsecurity=False).hexdigest()
    assert await _md5_matches(None, username, f"md5{digest}", password) is True
    assert await _md5_matches(None, username, f"md5{digest}", "other") is False


# --- setting parsing -------------------------------------------------------


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        ('"public, audit"', "public, audit"),
        ("public", "public"),
        ('"has ""inner"" quotes"', 'has "inner" quotes'),
        ('""', ""),
        ('"unbalanced', '"unbalanced'),
    ],
)
def test_unquote_setting(stored: str, expected: str) -> None:
    assert unquote_setting(stored) == expected


def test_parse_setconfig_handles_quoted_and_plain_values() -> None:
    assert parse_setconfig(
        ['search_path="public, audit"', "work_mem=64MB", "role=app_owner"]
    ) == {
        "search_path": "public, audit",
        "work_mem": "64MB",
        "role": "app_owner",
    }


def test_parse_setconfig_tolerates_junk_and_none() -> None:
    assert parse_setconfig(None) == {}
    assert parse_setconfig(["no-equals-sign"]) == {}


def test_parse_setconfig_keeps_equals_signs_inside_values() -> None:
    assert parse_setconfig(["options=-c a=b"]) == {"options": "-c a=b"}
