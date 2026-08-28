"""Account-local policy for starting EdgePilot trading runs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from edgepilot.platform.file_lock import FileLock
from edgepilot.platform.paths import strategies_state_root
from edgepilot.execution.run_state import load_run
from edgepilot.execution.run_state import register_running
from edgepilot.execution.run_state import running_runs


TRADING_MODES = frozenset({"paper", "demo", "live"})
MAX_ACTIVE_TRADING_RUNS = 1


class TradingRunActiveError(RuntimeError):
    """Raised when the account already owns the allowed trading run."""

    def __init__(self, active: dict[str, Any]) -> None:
        self.active = dict(active)
        strategy = str(active.get("strategy") or "another strategy")
        mode = str(active.get("mode") or "trading")
        super().__init__(
            f"{strategy} is already running in {mode} mode; stop it before starting another trading strategy",
        )

    def public_details(self) -> dict[str, Any]:
        return {"active": {
            key: self.active[key]
            for key in ("job_id", "run_id", "strategy", "mode")
            if self.active.get(key) is not None
        }}


def active_trading_runs() -> list[dict[str, Any]]:
    """Return active Paper, Demo, or Live runs for the current account."""
    root = strategies_state_root()
    if not root.is_dir():
        return []
    active: list[dict[str, Any]] = []
    for strategy_path in sorted(root.iterdir(), key=lambda path: path.name):
        runs_path = strategy_path / "runs"
        if not strategy_path.is_dir() or not runs_path.is_dir():
            continue
        for run_id, pid in running_runs(runs_path).items():
            try:
                record = load_run(runs_path, run_id)
            except (OSError, ValueError):
                continue
            mode = record.get("mode")
            if mode not in TRADING_MODES:
                continue
            active.append({
                "run_id": run_id,
                "strategy": str(record.get("strategy", {}).get("name") or strategy_path.name),
                "mode": mode,
                "pid": pid,
            })
    return active


def ensure_trading_slot_available() -> None:
    """Reject a new trading intent while the account has an active run."""
    active = active_trading_runs()
    if len(active) >= MAX_ACTIVE_TRADING_RUNS:
        raise TradingRunActiveError(active[0])


def claim_trading_slot(
    runs_path: Path,
    run_id: str,
    *,
    new_record: dict[str, Any] | None = None,
) -> None:
    """Atomically check the account-wide limit, save, and register one run."""
    lock_path = strategies_state_root() / ".locks" / "trading-start.lock"
    with FileLock(str(lock_path)):
        ensure_trading_slot_available()
        if new_record is not None:
            run_dir = runs_path / run_id
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "run.json").write_text(json.dumps(new_record, indent=2), encoding="utf-8")
        register_running(runs_path, run_id)
