"""Lightweight loading for the user-state environment file."""

from __future__ import annotations

import os
from pathlib import Path


def load_env(path: Path) -> None:
    """Load a user-state .env without overwriting explicit environment values."""
    if not path.exists():
        return
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
            os.environ.setdefault(key, raw)
