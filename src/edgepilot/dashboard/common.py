"""Standard-library helpers shared by the Dashboard and native worker."""

from __future__ import annotations

import re
from pathlib import Path


class ConfigConflictError(ValueError):
    """Raised when a user configuration would overwrite protected state."""


def safe_config_name(value: str) -> str:
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,79}", value):
        raise ValueError("configuration names use lowercase letters, numbers, underscores, and hyphens")
    return value


def safe_directory(parent: Path, name: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,160}", name):
        raise ValueError("invalid local item name")
    path = (parent / name).resolve()
    if path.parent != parent.resolve():
        raise ValueError("invalid local path")
    return path
