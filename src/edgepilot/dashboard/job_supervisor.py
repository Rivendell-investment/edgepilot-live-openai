"""Durable state and recovery for Dashboard-managed subprocess jobs.

The localhost HTTP adapter and command builders intentionally remain outside
this module. This module is the single owner of the in-memory job registry and
its ``dashboard-jobs/*.json`` persistence format.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
import tempfile
from threading import Lock
from typing import Any, Callable

from edgepilot.platform.paths import active_account_key, state_root
from edgepilot.execution.run_state import pid_matches, running_runs


LOGGER = logging.getLogger("edgepilot.dashboard.jobs")
JOBS: dict[str, dict[str, Any]] = {}
JOBS_LOCK = Lock()
JOB_STORE: Path | None = None
JOB_STORE_IDENTITY: dict[str, Any] = {}

_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[@-Z\\-_]")
_NAUTILUS_ERROR_RE = re.compile(r"^\[ERROR\]\s+\S+:\s+(.+)$")


class JobNotFound(LookupError):
    """The requested job is not owned by the active account."""


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def job_view(job: dict[str, Any]) -> dict[str, Any]:
    """Return only serializable, safe job fields to the browser."""
    return {
        key: value
        for key, value in job.items()
        if not key.startswith("_") and key != "owner_account_key"
    }


def write_job_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(value, output, sort_keys=True, separators=(",", ":"), default=str)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        if os.name != "nt":
            os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def persist_job_locked(job_id: str) -> None:
    if JOB_STORE is None or job_id not in JOBS:
        return
    job = {key: value for key, value in JOBS[job_id].items() if not key.startswith("_")}
    job.setdefault("service_instance_id", JOB_STORE_IDENTITY.get("instance_nonce"))
    job.setdefault("build_id", JOB_STORE_IDENTITY.get("build_id"))
    try:
        write_job_atomic(JOB_STORE / f"{job_id}.json", job)
    except OSError:
        LOGGER.exception(
            "Dashboard job state could not be persisted",
            extra={
                "event": "dashboard.job.persist_failed",
                "job_id": job_id,
                "result": "failed",
            },
        )


def find_persisted_job_run(root: Path, job_id: str) -> str | None:
    strategy_roots = [root / "strategies"]
    accounts = root / "accounts"
    if accounts.is_dir():
        strategy_roots.extend(
            path / "strategies" for path in accounts.iterdir() if path.is_dir()
        )
    matches: list[str] = []
    for strategies in strategy_roots:
        executions = (
            strategies.glob("*/runs/*/execution.json")
            if strategies.is_dir()
            else ()
        )
        for execution in executions:
            value = read_json(execution, {})
            if isinstance(value, dict) and value.get("job_id") == job_id:
                matches.append(execution.parent.name)
    return matches[0] if len(matches) == 1 else None


def configure_job_store(directory: Path, identity: dict[str, Any]) -> None:
    """Restore durable jobs when the single Live service starts."""
    global JOB_STORE, JOB_STORE_IDENTITY
    directory.mkdir(parents=True, exist_ok=True)
    JOB_STORE = directory
    JOB_STORE_IDENTITY = dict(identity)
    restored: dict[str, dict[str, Any]] = {}
    for path in sorted(directory.glob("job-*.json")):
        value = read_json(path, None)
        if not isinstance(value, dict) or value.get("job_id") != path.stem:
            continue
        job = dict(value)
        output_path = directory / f"{path.stem}.log"
        if output_path.is_file():
            try:
                with output_path.open("rb") as output:
                    output.seek(max(0, output_path.stat().st_size - 12000))
                    job["output"] = strip_ansi(
                        output.read(12000).decode("utf-8", errors="replace"),
                    )
                job["output_path"] = str(output_path)
            except OSError:
                pass
        if not isinstance(job.get("run_id"), str):
            discovered = find_persisted_job_run(directory.parent, path.stem)
            if discovered is not None:
                job["run_id"] = discovered
        if job.get("status") in {"QUEUED", "RUNNING", "STOPPING"}:
            pid = job.get("pid")
            if pid_matches(pid, job.get("process_start_token")):
                job.update(
                    recovered=True,
                    message="本地服务已恢复对此任务的观察；停止将通过运行记录协调。",
                )
            else:
                job.update(
                    status="FAILED",
                    stage="lost",
                    message="任务进程已经退出。",
                    finished_at=datetime.now(timezone.utc).isoformat(),
                    returncode=None,
                    error="本地服务重启后未找到对应任务进程。",
                    error_code="JOB_PROCESS_LOST",
                )
        restored[path.stem] = job
    with JOBS_LOCK:
        JOBS.clear()
        JOBS.update(restored)
        for job_id in restored:
            persist_job_locked(job_id)


def job_belongs_to_active_account(job: dict[str, Any]) -> bool:
    return job.get("owner_account_key") == active_account_key()


def reconcile_recovered_jobs_locked() -> None:
    """Resolve stale durable jobs before they are reported as active."""
    for job_id, job in JOBS.items():
        if (
            job.get("status") not in {"QUEUED", "RUNNING", "STOPPING"}
            or job.get("_process") is not None
        ):
            continue
        pid = job.get("pid")
        if type(pid) is not int or pid_matches(pid, job.get("process_start_token")):
            continue
        stopping = job.get("status") == "STOPPING"
        job.update(
            status="STOPPED" if stopping else "FAILED",
            stage="stopped" if stopping else "lost",
            message="任务已停止。" if stopping else "任务进程已经退出。",
            finished_at=datetime.now(timezone.utc).isoformat(),
            returncode=None,
            error=None if stopping else "本地服务检测到任务进程已经退出。",
            error_code=None if stopping else "JOB_PROCESS_LOST",
        )
        persist_job_locked(job_id)


def all_active_work() -> bool:
    """Return service-wide work, including every authenticated account."""
    with JOBS_LOCK:
        reconcile_recovered_jobs_locked()
        if any(
            job.get("status") in {"QUEUED", "RUNNING", "STOPPING"}
            for job in JOBS.values()
        ):
            return True
    roots = [state_root() / "strategies"]
    accounts = state_root() / "accounts"
    if accounts.is_dir():
        roots.extend(path / "strategies" for path in accounts.iterdir() if path.is_dir())
    for strategies in roots:
        if not strategies.is_dir():
            continue
        for registry in strategies.glob("*/runs/running.json"):
            if running_runs(registry.parent):
                return True
    return False


def strip_ansi(text: str) -> str:
    """Remove terminal color and control sequences from subprocess output."""
    return _ANSI_ESCAPE.sub("", text)


def job_error(output: str, returncode: int) -> str:
    """Extract the CLI's user-facing error without exposing arbitrary logs."""
    nautilus_error: str | None = None
    for line in reversed(strip_ansi(output).splitlines()):
        message = line.strip()
        if message.lower().startswith("error:"):
            detail = message.split(":", 1)[1].strip()
            if detail:
                return detail
        if nautilus_error is None:
            match = _NAUTILUS_ERROR_RE.match(message)
            if match:
                nautilus_error = match.group(1).strip()
    if nautilus_error:
        return nautilus_error
    return f"Process exited with code {returncode}"


def job_run_id(output: str) -> str | None:
    """Extract the run identifier emitted by both JSON and trading commands."""
    match = re.search(r'"run_id"\s*:\s*"([^"\r\n]+)"', output)
    if match:
        return match.group(1)
    match = re.search(r"(?m)^Run:\s*([^\s]+)\s*$", strip_ansi(output))
    return match.group(1) if match else None


def prestart_failure_message(phase: str) -> str:
    if phase == "runtime_prepare":
        return "运行环境安装失败，可安全重试；推荐和历史结果不受影响。"
    if phase == "command_resolution":
        return "策略或配置解析失败，请检查所选策略和配置后重试。"
    return "任务进程启动失败，请检查本地运行环境后重试。"


def execution_failure_message(kind: str) -> str:
    if kind == "backtest":
        return "回测执行失败；可查看任务输出后重试。"
    if kind == "data":
        return "数据下载失败；可查看任务输出后重试。"
    return "交易任务执行失败；可查看任务输出后重试。"


def list_active_account_jobs() -> list[dict[str, Any]]:
    with JOBS_LOCK:
        reconcile_recovered_jobs_locked()
        return [
            job_view(job)
            for job in JOBS.values()
            if job_belongs_to_active_account(job)
        ]


def get_active_account_job(job_id: str) -> dict[str, Any]:
    with JOBS_LOCK:
        reconcile_recovered_jobs_locked()
        job = JOBS.get(job_id)
        if job is None or not job_belongs_to_active_account(job):
            raise JobNotFound(job_id)
        return job_view(job)


def stop_active_account_job(
    job_id: str,
    *,
    emergency_stop_run: Callable[[str], None],
    terminate_process: Callable[[Any, str], None],
    terminate_recovered: Callable[[int, str, str], None],
) -> tuple[dict[str, Any], int]:
    """Apply the existing idempotent stop state machine for one owned job."""
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is not None and not job_belongs_to_active_account(job):
            job = None
        process = job.get("_process") if job else None
        if job is None:
            raise JobNotFound(job_id)
        if job.get("status") == "STOPPING":
            return {"job_id": job_id, "status": "STOPPING"}, 202
        if job.get("status") not in {"QUEUED", "RUNNING"}:
            return {"job_id": job_id, "status": job.get("status")}, 200
        if process is None:
            run_id = job.get("run_id")
            if isinstance(run_id, str):
                emergency_stop_run(run_id)
                job.update(
                    status="STOPPING",
                    stage="stopping",
                    message="正在通过运行记录停止已恢复的策略",
                )
                persist_job_locked(job_id)
                return {"job_id": job_id, "status": "STOPPING"}, 202
            recovered_pid = job.get("pid")
            recovered_token = job.get("process_start_token")
            if (
                type(recovered_pid) is int
                and isinstance(recovered_token, str)
                and pid_matches(recovered_pid, recovered_token)
            ):
                job.update(
                    status="STOPPING",
                    stage="stopping",
                    message="正在停止已恢复的任务进程",
                )
                persist_job_locked(job_id)
                terminate_recovered(recovered_pid, recovered_token, job_id)
                return {"job_id": job_id, "status": "STOPPING"}, 202
            job.update(
                status="FAILED",
                stage="lost",
                error="任务监督句柄已经丢失，无法安全终止未知进程。",
                error_code="JOB_SUPERVISOR_LOST",
                finished_at=datetime.now(timezone.utc).isoformat(),
            )
            persist_job_locked(job_id)
            return {"job_id": job_id, "status": "FAILED"}, 200
        if process.poll() is not None:
            job.update(
                status="FAILED",
                stage="failed",
                returncode=process.returncode,
                error=job_error(job.get("output", ""), process.returncode or 1),
                error_code="JOB_PROCESS_EXITED",
                finished_at=datetime.now(timezone.utc).isoformat(),
            )
            job.pop("_process", None)
            persist_job_locked(job_id)
            return {"job_id": job_id, "status": "FAILED"}, 200
        job.update(
            status="STOPPING",
            stage="stopping",
            message="正在停止任务进程",
        )
        persist_job_locked(job_id)
        terminate_process(process, job_id)
    return {"job_id": job_id, "status": "STOPPING"}, 202
