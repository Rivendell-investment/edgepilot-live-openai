"""Lightweight process and run-state helpers shared by the Dashboard and runtime."""

from __future__ import annotations

import json
import logging
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4


RUNNING_FILE = "running.json"
RUNNING_LOCK_FILE = ".running.lock"
RUNTIME_FILE = "runtime.json"
EXECUTION_FILE = "execution.json"
EMERGENCY_STOP_FILE = "emergency-stop.json"
LOGGER = logging.getLogger("edgepilot.run_state")


def load_run(runs_path: Path, run_id: str) -> dict[str, Any]:
    """Read a saved run without importing the native trading engine."""
    path = runs_path / run_id / "run.json"
    if not path.exists():
        raise FileNotFoundError(f"Unknown run: {run_id}")
    return json.loads(path.read_text(encoding="utf-8"))


def running_runs(runs_path: Path) -> dict[str, int]:
    """Return currently running run IDs and remove stale PID entries."""
    with _running_lock(runs_path):
        return _running_runs_unlocked(runs_path)


def load_execution(runs_path: Path, run_id: str) -> dict[str, Any]:
    """Return the persisted execution outcome for a trading run."""
    try:
        stored = json.loads((runs_path / run_id / EXECUTION_FILE).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return stored if isinstance(stored, dict) else {}


def run_status(runs_path: Path, run_id: str, mode: object, *, active: dict[str, int] | None = None) -> str:
    """Resolve a run status from live process state and its persisted outcome."""
    current = running_runs(runs_path) if active is None else active
    if run_id in current:
        return "RUNNING"
    if mode == "backtest":
        return "COMPLETE"
    return "FAILED" if load_execution(runs_path, run_id).get("status") == "FAILED" else "STOPPED"


def register_running(runs_path: Path, run_id: str) -> None:
    pid = os.getpid()
    with _running_lock(runs_path):
        active = _running_runs_unlocked(runs_path)
        if run_id in active:
            raise RuntimeError(f"Run is already running: {run_id}")
        active[run_id] = pid
        _write_json_atomic(runs_path / RUNNING_FILE, active)
        try:
            _write_json_atomic(runs_path / run_id / EXECUTION_FILE, {
                "status": "RUNNING",
                "pid": pid,
                "started_at": datetime.now(timezone.utc).isoformat(),
            })
        except OSError:
            active.pop(run_id, None)
            _write_json_atomic(runs_path / RUNNING_FILE, active)
            raise


def unregister_running(runs_path: Path, run_id: str) -> None:
    with _running_lock(runs_path):
        active = _running_runs_unlocked(runs_path)
        # A late cleanup from an older process must not unregister a newer owner.
        if active.get(run_id) == os.getpid():
            active.pop(run_id, None)
            _write_json_atomic(runs_path / RUNNING_FILE, active)


def record_execution_result(runs_path: Path, run_id: str, *, failed: BaseException | None = None) -> None:
    """Persist a safe terminal result so failures survive Dashboard restarts."""
    previous = load_execution(runs_path, run_id)
    payload: dict[str, Any] = {
        "status": "FAILED" if failed is not None else "STOPPED",
        "pid": os.getpid(),
        "started_at": previous.get("started_at"),
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }
    if failed is not None:
        from edgepilot.app_logging import redact

        payload["error"] = {
            "code": type(failed).__name__,
            "message": redact(str(failed) or type(failed).__name__),
        }
    _write_json_atomic(runs_path / run_id / EXECUTION_FILE, payload)


def request_emergency_stop(runs_path: Path, run_id: str) -> None:
    """Ask a local node to stop gracefully without importing NautilusTrader."""
    directory = runs_path / run_id
    if not directory.is_dir():
        raise FileNotFoundError(f"Unknown run: {run_id}")
    (directory / EMERGENCY_STOP_FILE).write_text(
        json.dumps({"requested_at": datetime.now(timezone.utc).isoformat()}),
        encoding="utf-8",
    )
    LOGGER.warning(
        "emergency stop requested",
        extra={"event": "trading.emergency_stop.requested", "run_id": run_id, "result": "success"},
    )


def _running_runs_unlocked(runs_path: Path) -> dict[str, int]:
    path = runs_path / RUNNING_FILE
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        stored = {}
    if not isinstance(stored, dict):
        stored = {}
    active: dict[str, int] = {}
    stale: list[tuple[str, int]] = []
    for run_id, pid in stored.items():
        try:
            parsed_pid = int(pid)
        except (TypeError, ValueError):
            continue
        if _pid_exists(parsed_pid):
            active[str(run_id)] = parsed_pid
        else:
            stale.append((str(run_id), parsed_pid))
    if active != stored:
        try:
            _write_json_atomic(path, active)
        except OSError:
            pass
    for run_id, pid in stale:
        _record_unexpected_exit(runs_path, run_id, pid)
    return active


def _record_unexpected_exit(runs_path: Path, run_id: str, pid: int) -> None:
    outcome = load_execution(runs_path, run_id)
    if outcome.get("status") != "RUNNING" or outcome.get("pid") != pid:
        return
    outcome.update({
        "status": "FAILED",
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "error": {
            "code": "PROCESS_EXITED_UNEXPECTEDLY",
            "message": "The strategy process exited without reporting a final status.",
        },
    })
    try:
        _write_json_atomic(runs_path / run_id / EXECUTION_FILE, outcome)
    except OSError:
        LOGGER.exception(
            "unexpected process exit could not be recorded",
            extra={"event": "trading.process_exit.persist_failed", "run_id": run_id, "result": "failed"},
        )
        return
    LOGGER.error(
        "strategy process exited without a final status",
        extra={
            "event": "trading.process_exit.unexpected",
            "run_id": run_id,
            "result": "failed",
            "params": {"pid": pid},
        },
    )


@contextmanager
def _running_lock(runs_path: Path) -> Iterator[None]:
    """Serialize the running registry across Dashboard-launched processes."""
    runs_path.mkdir(parents=True, exist_ok=True)
    lock_path = runs_path / RUNNING_LOCK_FILE
    with lock_path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _write_json_atomic(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        process_query_limited_information = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            process_query_limited_information,
            False,
            pid,
        )
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True
