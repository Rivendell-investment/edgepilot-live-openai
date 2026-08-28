"""Lightweight loading for the user-state environment file."""

from __future__ import annotations

import os
from pathlib import Path


def read_env(path: Path) -> dict[str, str]:
    """Read a simple user-state environment file without mutating the process."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        value = line.strip()
        if not value or value.startswith("#"):
            continue
        if value.startswith("export "):
            value = value[7:].lstrip()
        if "=" not in value:
            continue
        key, raw = value.split("=", 1)
        key = key.strip()
        raw = raw.strip()
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in {"'", '"'}:
            raw = raw[1:-1]
        if key:
            values[key] = raw
    return values


def load_env(path: Path) -> None:
    """Load a user-state .env without overwriting explicit environment values."""
    for key, raw in read_env(path).items():
        os.environ.setdefault(key, raw)
