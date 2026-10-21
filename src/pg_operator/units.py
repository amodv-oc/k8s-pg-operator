"""Kubernetes-style quantities and durations, as written in specs.

``maxLag: 1Gi`` and ``maxSlotInactive: 10m`` read the way every other Kubernetes
field does. The CRD constrains their shape with a pattern; these turn them into
bytes and seconds, and reject what the pattern alone cannot (a zero).
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

#: Binary (Ki, Mi, ...) and decimal (k, M, ...) suffixes, as resource.Quantity.
_QUANTITY_SUFFIXES = {
    "": 1,
    "Ki": 1024,
    "Mi": 1024**2,
    "Gi": 1024**3,
    "Ti": 1024**4,
    "Pi": 1024**5,
    "k": 1000,
    "M": 1000**2,
    "G": 1000**3,
    "T": 1000**4,
    "P": 1000**5,
}

#: Mirrored by the CRD's ``pattern`` for string quantities.
QUANTITY_PATTERN = r"^[0-9]+(\.[0-9]+)?(Ki|Mi|Gi|Ti|Pi|k|M|G|T|P)?$"
_QUANTITY = re.compile(QUANTITY_PATTERN)

#: Go-style durations without fractions: ``90s``, ``10m``, ``1h30m``.
DURATION_PATTERN = r"^([0-9]+h)?([0-9]+m)?([0-9]+s)?$"
_DURATION = re.compile(DURATION_PATTERN)


def parse_quantity(value: int | str) -> int:
    """Bytes in a quantity such as ``1Gi``, ``512Mi`` or ``1073741824``."""
    if isinstance(value, int):
        if value < 0:
            raise ValueError(f"quantity {value} is negative")
        return value
    match = _QUANTITY.match(value)
    if match is None:
        raise ValueError(
            f"quantity {value!r} is not a number with an optional suffix "
            "(Ki, Mi, Gi, Ti, Pi, k, M, G, T, P)"
        )
    suffix = match.group(2) or ""
    number = value[: len(value) - len(suffix)]
    try:
        amount = Decimal(number) * _QUANTITY_SUFFIXES[suffix]
    except InvalidOperation as exc:  # pragma: no cover - the pattern prevents it
        raise ValueError(f"quantity {value!r} is not a number") from exc
    return int(amount)


def parse_duration(value: str) -> int:
    """Seconds in a duration such as ``10m``, ``90s`` or ``1h30m``."""
    match = _DURATION.match(value)
    if not value or match is None:
        raise ValueError(
            f"duration {value!r} must be hours, minutes and/or seconds, e.g. 10m or 1h30m"
        )
    hours, minutes, seconds = (int(part[:-1]) if part else 0 for part in match.groups())
    return hours * 3600 + minutes * 60 + seconds


_BINARY_UNITS = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")


def format_bytes(value: int) -> str:
    """``12 KiB``, ``1.5 GiB`` - for status a person reads; status keeps the bytes too."""
    amount = float(value)
    for unit in _BINARY_UNITS:
        if abs(amount) < 1024 or unit == _BINARY_UNITS[-1]:
            if unit == "B":
                return f"{int(amount)} B"
            return f"{amount:.1f} {unit}".replace(".0 ", " ")
        amount /= 1024
    raise AssertionError("unreachable")  # pragma: no cover
