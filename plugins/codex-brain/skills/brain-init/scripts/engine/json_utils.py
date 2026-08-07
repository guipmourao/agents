"""Strict JSON parsing for configuration and runtime authority surfaces."""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from typing import Any


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON object key: {key}")
        value[key] = item
    return value


def _reject_nonfinite_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number is not permitted: {value}")


def parse_finite_json_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"non-finite JSON number is not permitted: {value}")
    return parsed


_FRACTION_RE = re.compile(r"\.(\d+)(?=[+-]\d{2}:\d{2}$|$)")


def normalize_iso_timestamp(value: str) -> str:
    """Normalize a ``Z``-suffixed ISO-8601 timestamp for ``datetime.fromisoformat``.

    ``fromisoformat`` only accepts 0, 3, or 6 fractional-second digits, but
    common timestamp sources (e.g. PowerShell's ``Get-Date -Format o``, which
    emits 7) do not respect that. Pad or truncate any fractional part to 6
    digits (microseconds) so those timestamps still parse.
    """
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    match = _FRACTION_RE.search(normalized)
    if match:
        digits = (match.group(1) + "000000")[:6]
        normalized = (
            normalized[: match.start()] + "." + digits + normalized[match.end() :]
        )
    return normalized


def canonicalize_iso_timestamp(value: str) -> str:
    """Canonicalize an ISO-8601 timestamp to whole-second UTC, ``Z``-suffixed.

    Accepts any timezone offset and any fractional-second precision (see
    :func:`normalize_iso_timestamp`) and truncates to whole seconds, matching
    the ``YYYY-MM-DDTHH:MM:SSZ`` form that ledgers and templates require.
    """
    parsed = datetime.fromisoformat(normalize_iso_timestamp(value))
    if parsed.tzinfo is None:
        raise ValueError("generated_at must include a UTC offset or 'Z' suffix")
    return (
        parsed.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def strict_json_loads(value: str | bytes | bytearray) -> Any:
    """Parse JSON while rejecting duplicate object keys at every depth."""

    return json.loads(
        value,
        object_pairs_hook=_object_without_duplicates,
        parse_constant=_reject_nonfinite_constant,
        parse_float=parse_finite_json_float,
    )
