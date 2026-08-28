"""Explicit assignment of pre-account Live state to the active account."""

from __future__ import annotations

from pathlib import Path
import secrets
import shutil
from typing import Any, Callable


class LegacyStateConflict(RuntimeError):
    """The legacy state cannot be assigned without overwriting active state."""


def legacy_state_summary(root: Path) -> dict[str, Any]:
    legacy_strategies = root / "strategies"
    strategy_count = (
        sum(
            1
            for path in legacy_strategies.iterdir()
            if path.is_dir() and path.name != ".locks"
        )
        if legacy_strategies.is_dir()
        else 0
    )
    credentials = root / ".env"
    return {
        "available": strategy_count > 0 or credentials.is_file(),
        "strategy_count": strategy_count,
        "has_credentials": credentials.is_file(),
    }

def legacy_has_active_runs(
    root: Path,
    running: Callable[[Path], dict[str, int]],
) -> bool:
    strategies = root / "strategies"
    if not strategies.is_dir():
        return False
    for strategy in strategies.iterdir():
        runs = strategy / "runs"
        if runs.is_dir() and running(runs):
            return True
    return False


def claim_legacy_state(
    *,
    root: Path,
    destination_strategies: Path,
    destination_credentials: Path,
    summary: dict[str, Any],
    has_active_runs: bool,
) -> dict[str, Any]:
    """Assign legacy state while restoring all moved paths after a failure."""
    source_strategies = root / "strategies"
    source_credentials = root / ".env"
    if not summary["available"]:
        return {"claimed": False, "reason": "NO_LEGACY_STATE"}
    if has_active_runs:
        raise LegacyStateConflict("LEGACY_ACTIVE_WORK")
    destination_entries = (
        list(destination_strategies.iterdir())
        if destination_strategies.is_dir()
        else []
    )
    meaningful_entries = [
        path
        for path in destination_entries
        if path.name not in {"__init__.py", ".locks"}
    ]
    if meaningful_entries or destination_credentials.exists():
        raise LegacyStateConflict("ACCOUNT_STATE_NOT_EMPTY")
    temporary = destination_strategies.parent / f".legacy-empty-{secrets.token_hex(8)}"
    moved_strategies = False
    moved_credentials = False
    try:
        if destination_strategies.exists():
            destination_strategies.replace(temporary)
        if source_strategies.exists():
            source_strategies.replace(destination_strategies)
            moved_strategies = True
        if source_credentials.exists():
            destination_credentials.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            source_credentials.replace(destination_credentials)
            moved_credentials = True
        if temporary.exists():
            shutil.rmtree(temporary)
    except Exception:
        if moved_credentials and destination_credentials.exists():
            destination_credentials.replace(source_credentials)
        if moved_strategies and destination_strategies.exists():
            destination_strategies.replace(source_strategies)
        if temporary.exists():
            temporary.replace(destination_strategies)
        raise
    return {
        "claimed": True,
        "strategy_count": summary["strategy_count"],
        "credentials_moved": moved_credentials,
    }
