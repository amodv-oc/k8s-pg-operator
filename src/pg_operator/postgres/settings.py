"""Parsing of ``pg_db_role_setting.setconfig`` entries.

PostgreSQL stores per-database and per-role settings as ``name=value`` strings,
quoting the value when it contains characters that would otherwise be ambiguous
— a comma, whitespace, a quote. ``ALTER DATABASE x SET search_path TO 'a, b'``
comes back as ``search_path="a, b"``.

Comparing the raw stored form against the spec would therefore never match for
such values, and the operator would re-issue the same ALTER on every reconcile:
harmless, but it means a steady-state pass is never actually a no-op and the
status churns "converged" messages forever. Unquoting here is what makes drift
detection settle.
"""

from __future__ import annotations


def unquote_setting(value: str) -> str:
    """Undo the quoting PostgreSQL applies when storing a setting value."""
    if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
        return value[1:-1].replace('""', '"')
    return value


def parse_setconfig(entries: list[str] | None) -> dict[str, str]:
    """Turn a ``setconfig`` array into a ``{name: value}`` mapping."""
    settings: dict[str, str] = {}
    for entry in entries or []:
        name, separator, value = str(entry).partition("=")
        if not separator:
            continue
        settings[name.strip()] = unquote_setting(value)
    return settings
