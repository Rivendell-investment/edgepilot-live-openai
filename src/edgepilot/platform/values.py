from __future__ import annotations

import json
from typing import Any


def parse_value(raw: str) -> Any:
    """Parse a CLI value as JSON when possible, otherwise preserve it as text."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def parse_assignments(values: list[str] | None) -> dict[str, Any]:
    parsed: dict[str, Any] = {}
    for assignment in values or []:
        if "=" not in assignment:
            raise ValueError(f"Expected KEY=VALUE, received {assignment!r}")
        key, value = assignment.split("=", 1)
        if not key:
            raise ValueError(f"Expected a non-empty key in {assignment!r}")
        parsed[key] = parse_value(value)
    return parsed
