"""Stable user-owned paths for EdgePilot runtime state."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator


def state_root() -> Path:
    """Return persistent state independently of the plugin cache location."""
    configured = os.environ.get("EDGEPILOT_HOME")
    if configured:
        return Path(configured).expanduser()
    if os.name == "nt":
        app_data = os.environ.get("APPDATA")
        return (Path(app_data) if app_data else Path.home()) / "EdgePilot"
    return Path.home() / ".edgepilot"


def strategies_state_root() -> Path:
    """Return the user-owned strategy package directory."""
    return state_root() / "strategies"


def strategy_runs_path(strategy_name: str) -> Path:
    """Return a strategy's own persistent run directory."""
    if not strategy_name or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for character in strategy_name):
        raise ValueError("invalid strategy name")
    path = strategies_state_root() / strategy_name / "runs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def iter_run_directories() -> Iterator[Path]:
    """Yield every persisted run, grouped beneath its owning strategy."""
    root = strategies_state_root()
    if not root.exists():
        return
    for strategy_path in root.iterdir():
        runs = strategy_path / "runs"
        if not strategy_path.is_dir() or not runs.is_dir():
            continue
        for candidate in runs.iterdir():
            if candidate.is_dir() and (candidate / "run.json").is_file():
                yield candidate


def find_run_directory(run_id: str) -> Path:
    """Find a globally unique run ID without introducing a global runs folder."""
    matches = [path for path in iter_run_directories() if path.name == run_id]
    if len(matches) != 1:
        raise FileNotFoundError(f"Unknown run: {run_id}")
    return matches[0]
