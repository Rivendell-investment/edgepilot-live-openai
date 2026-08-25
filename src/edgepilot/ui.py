"""Small localhost dashboard for inspecting and starting EdgePilot runs."""

from __future__ import annotations

import csv
import json
import logging
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import secrets
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock, Thread
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from edgepilot.dashboard_common import ConfigConflictError
from edgepilot.dashboard_common import safe_config_name
from edgepilot.dashboard_common import safe_directory
from edgepilot.env_file import load_env
from edgepilot.marketplace import MarketplaceRequestError, install_package
from edgepilot.paths import state_root
from edgepilot.paths import find_run_directory
from edgepilot.paths import iter_run_directories
from edgepilot.paths import strategy_runs_path
from edgepilot.paths import strategies_state_root
from edgepilot.run_state import request_emergency_stop
from edgepilot.run_state import load_execution
from edgepilot.run_state import running_runs
from edgepilot.run_state import run_status
from edgepilot.app_logging import configure_logging
from edgepilot.locale import SUPPORTED_LANGUAGES, normalize_supported_locale
from edgepilot import auth


def parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return (parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed).astimezone(timezone.utc)


ASSETS = Path(__file__).with_name("ui_assets")
STATE = state_root()
CATALOG_ROOT = STATE / "catalog"
CATALOG = CATALOG_ROOT / "data"
JOBS: dict[str, dict[str, Any]] = {}
JOBS_LOCK = Lock()
RUNTIME_INSTALL_LOCK = Lock()

# The dashboard process launches child CLI commands, so it needs the same
# local-only environment values as the CLI itself. Values never leave this
# process or appear in an API response.
load_env(STATE / ".env")
LOGGER = configure_logging()


def _run_login_worker(started: dict[str, Any]) -> None:
    def poll() -> None:
        try:
            auth.poll_login(started)
        except auth.AuthError as exc:
            LOGGER.warning("dashboard login failed", extra={"event": "dashboard.auth.login_failed", "result": "failed",
                           "params": {"code": str(exc), "stage": exc.stage or "unknown", **exc.diagnostics}})

    Thread(target=poll, daemon=True).start()


def _plugin_environment() -> dict[str, str]:
    from edgepilot.runtime import plugin_root
    root = plugin_root()
    core = root / "core_src"
    if not core.is_dir():
        core = root.parent / "edgepilot-core" / "src"
    environment = dict(os.environ)
    python_path = [str(root / "src"), str(core)]
    if environment.get("PYTHONPATH"):
        python_path.append(environment["PYTHONPATH"])
    environment["PYTHONPATH"] = os.pathsep.join(python_path)
    return environment


def _runtime_dashboard_call(operation: str, payload: dict[str, Any]) -> Any:
    """Execute Nautilus-dependent Dashboard work in the locked runtime."""
    from edgepilot.runtime import active_runtime_python

    completed = subprocess.run(
        [str(active_runtime_python()), "-m", "edgepilot.dashboard_worker"],
        input=json.dumps({"operation": operation, "payload": payload}, separators=(",", ":")),
        cwd=STATE,
        env=_plugin_environment(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    try:
        response = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        LOGGER.error(
            "Dashboard runtime worker returned invalid output",
            extra={"event": "dashboard.runtime_worker.failed", "result": "failed", "params": {"operation": operation, "returncode": completed.returncode}},
        )
        raise RuntimeError("Dashboard runtime worker returned an invalid response") from error
    if completed.returncode != 0 or not isinstance(response, dict) or response.get("ok") is not True:
        failure = response.get("error") if isinstance(response, dict) else None
        error_type = failure.get("type") if isinstance(failure, dict) else None
        message = failure.get("message") if isinstance(failure, dict) else None
        message = str(message or "Dashboard runtime operation failed")
        if error_type == "FileNotFoundError":
            raise FileNotFoundError(message)
        if error_type == "ConfigConflictError":
            raise ConfigConflictError(message)
        if error_type in {"ValueError", "TypeError", "ModuleNotFoundError"}:
            raise ValueError(message)
        raise RuntimeError(message)
    return response.get("result")


def _json(handler: BaseHTTPRequestHandler, payload: Any, status: int = 200) -> None:
    body = json.dumps(payload, default=str).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _error(handler: BaseHTTPRequestHandler, code: str, message: str, status: int, **details: Any) -> None:
    """Return a stable UI error code while preserving the diagnostic message."""
    _json(handler, {"error": {"code": code, "message": message, **details}}, status)


_MARKETPLACE_ERROR_RESPONSES = {
    "INVALID_ARGUMENT": (400, "the recommendation request is invalid"),
    "INVALID_REQUEST": (400, "the recommendation request is invalid"),
    "AUTH_REQUIRED": (401, "authentication is required"),
    "INVALID_TOKEN": (401, "authentication has expired"),
    "INSUFFICIENT_SCOPE": (403, "permission to use recommendations is required"),
    "CATALOG_COVERAGE_INSUFFICIENT": (409, "the strategy catalog cannot provide three compatible recommendations"),
    "RATE_LIMITED": (429, "too many recommendation requests; try again later"),
    "DOWNLOAD_QUOTA_EXCEEDED": (429, "strategy download quota exceeded"),
    "DOWNLOAD_QUOTA_UNAVAILABLE": (503, "strategy download quota service is unavailable"),
    "SERVICE_UNAVAILABLE": (503, "the recommendation service is unavailable"),
    "AUTH_SERVICE_UNAVAILABLE": (503, "the authentication service is unavailable"),
}


def _marketplace_request_error(handler: BaseHTTPRequestHandler, exc: MarketplaceRequestError) -> None:
    status, message = _MARKETPLACE_ERROR_RESPONSES.get(exc.code, (500, "the recommendation request failed"))
    code = exc.code if exc.code in _MARKETPLACE_ERROR_RESPONSES else "INTERNAL_ERROR"
    _error(handler, code, message, status, **exc.public_details())


def _dashboard_config(language: str | None) -> dict[str, Any]:
    from edgepilot.runtime import runtime_install_info, runtime_status
    runtime = runtime_status()
    if not runtime.get("installed"):
        try: runtime.update(runtime_install_info())
        except Exception as error: runtime["unavailable_reason"] = str(error)
    return {"language": normalize_supported_locale(language), "supported_languages": list(SUPPORTED_LANGUAGES), "runtime": runtime}


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _run_records() -> list[dict[str, Any]]:
    records = []
    for path in sorted(iter_run_directories(), reverse=True):
        record = _read_json(path / "run.json", None)
        if not isinstance(record, dict):
            LOGGER.warning(
                "invalid local run skipped",
                extra={"event": "dashboard.run.invalid", "result": "skipped", "params": {"run_id": path.name}},
            )
            continue
        record["run_id"] = path.name
        active = running_runs(path.parent)
        record["status"] = run_status(path.parent, path.name, record.get("mode"), active=active)
        execution = load_execution(path.parent, path.name)
        if execution:
            record["execution"] = execution
        records.append(record)
    return records


def _safe_manifest_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value.encode("utf-8")) > 64 * 1024 or any(ord(char) < 32 and char not in "\t\n\r" for char in value):
        return None
    return value


def _strategy_content(package: Path, internal_name: str, locale: str) -> dict[str, Any]:
    manifest = _read_json(package / "marketplace.json", {})
    if not isinstance(manifest, dict):
        manifest = {}
    english = {field: _safe_manifest_text(manifest.get(field)) for field in ("name", "summary", "description")}
    translated: dict[str, Any] = {}
    translations = manifest.get("translations")
    if isinstance(translations, dict):
        try:
            if len(json.dumps(translations, ensure_ascii=False).encode("utf-8")) > 64 * 1024:
                translations = None
        except (TypeError, ValueError):
            translations = None
    if locale != "en" and isinstance(translations, dict) and isinstance(translations.get(locale), dict):
        translated = translations[locale]
    fields = {
        field: _safe_manifest_text(translated.get(field)) or english[field]
        for field in ("name", "summary", "description")
    }
    translated_used = any(_safe_manifest_text(translated.get(field)) for field in fields)
    capacity = manifest.get("capacity") if isinstance(manifest.get("capacity"), dict) else {}
    markets = manifest.get("markets") if isinstance(manifest.get("markets"), dict) else {}
    assets = markets.get("assets") if isinstance(markets.get("assets"), list) else []
    risk_profile = manifest.get("risk_profile")
    return {
        "display_name": fields["name"] or internal_name,
        "summary": fields["summary"],
        "description": fields["description"],
        "content_locale": locale if translated_used else "en",
        "risk_profile": risk_profile if risk_profile in {"conservative", "balanced", "aggressive"} else None,
        "capacity_usd": capacity.get("usd") if isinstance(capacity.get("usd"), (int, float)) else None,
        "assets": [value for value in assets if isinstance(value, str) and value][:20],
    }


def _strategy_records(locale: str = "en") -> list[dict[str, Any]]:
    """Describe strategy packages without exposing implementation paths."""
    root = strategies_state_root()
    if not root.is_dir(): return []
    records = []
    for package in sorted(root.iterdir()):
        if not package.is_dir() or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,79}", package.name): continue
        try:
            marketplace = _read_json(package / ".marketplace.json", None)
            presets = sorted(path.stem for path in (package / "configs").glob("*.json")) if (package / "configs").is_dir() else []
            records.append({"name": package.name, "presets": presets, "source": "marketplace" if marketplace else "local",
                            "marketplace": marketplace} | _strategy_content(package, package.name, locale))
        except OSError:
            LOGGER.warning(
                "unreadable local strategy skipped",
                extra={"event": "dashboard.strategy.invalid", "result": "skipped", "params": {"strategy": package.name}},
            )
    return records


def _catalog_records() -> list[dict[str, Any]]:
    """Return a lightweight inventory of the native parquet catalog."""
    records: list[dict[str, Any]] = []
    if not CATALOG.exists():
        return records
    for kind_dir in sorted((p for p in CATALOG.iterdir() if p.is_dir()), key=lambda p: p.name):
        for dataset in sorted((p for p in kind_dir.iterdir() if p.is_dir()), key=lambda p: p.name):
            files = list(dataset.glob("*.parquet"))
            if not files:
                continue
            records.append({
                "kind": kind_dir.name,
                "name": dataset.name,
                "files": len(files),
                "bytes": sum(p.stat().st_size for p in files),
            })
    return records


def _job_view(job: dict[str, Any]) -> dict[str, Any]:
    """Return only serializable, safe job fields to the browser."""
    return {key: value for key, value in job.items() if not key.startswith("_")}


_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[@-Z\\-_]")
_NAUTILUS_ERROR_RE = re.compile(r"^\[ERROR\]\s+\S+:\s+(.+)$")


def _strip_ansi(text: str) -> str:
    """Remove terminal color and control sequences from subprocess output."""
    return _ANSI_ESCAPE.sub("", text)


def _job_error(output: str, returncode: int) -> str:
    """Extract the CLI's user-facing error without exposing arbitrary logs."""
    nautilus_error: str | None = None
    for line in reversed(_strip_ansi(output).splitlines()):
        message = line.strip()
        if message.lower().startswith("error:"):
            detail = message.split(":", 1)[1].strip()
            if detail:
                return detail
        if nautilus_error is None:
            m = _NAUTILUS_ERROR_RE.match(message)
            if m:
                nautilus_error = m.group(1).strip()
    if nautilus_error:
        return nautilus_error
    return f"Process exited with code {returncode}"


def _job_run_id(output: str) -> str | None:
    """Extract the run identifier emitted by both JSON and trading commands."""
    match = re.search(r'"run_id"\s*:\s*"([^"\r\n]+)"', output)
    if match:
        return match.group(1)
    match = re.search(r"(?m)^Run:\s*([^\s]+)\s*$", _strip_ansi(output))
    return match.group(1) if match else None


def _prestart_failure_message(phase: str) -> str:
    if phase == "runtime_prepare":
        return "运行环境安装失败，可安全重试；推荐和历史结果不受影响。"
    if phase == "command_resolution":
        return "策略或配置解析失败，请检查所选策略和配置后重试。"
    return "任务进程启动失败，请检查本地运行环境后重试。"


def _execution_failure_message(kind: str) -> str:
    if kind == "backtest":
        return "回测执行失败；可查看任务输出后重试。"
    if kind == "data":
        return "数据下载失败；可查看任务输出后重试。"
    return "交易任务执行失败；可查看任务输出后重试。"


def _start_job(
    *,
    kind: str,
    command: list[str] | Callable[[], list[str]] | None,
    prepare: Callable[[str], None] | None = None,
    starting_stage: str = "starting",
    starting_message: str = "启动任务",
) -> str:
    job_id = f"job-{__import__('datetime').datetime.now().strftime('%Y%m%d%H%M%S%f')}"
    with JOBS_LOCK:
        JOBS[job_id] = {
            "job_id": job_id,
            "kind": kind,
            "status": "QUEUED",
            "output": "",
            "run_id": None,
            "error": None,
            "stage": "preparing",
            "message": "准备运行环境" if prepare else "等待启动",
            "downloaded_bytes": 0,
            "total_bytes": None,
            "percent": None,
        }

    def run_process() -> None:
        LOGGER.info("job started", extra={"event": "dashboard.job.started", "job_id": job_id, "params": {"kind": kind}})
        with JOBS_LOCK:
            JOBS[job_id]["status"] = "RUNNING"
        phase = "runtime_prepare" if prepare is not None else "command_resolution"
        try:
            if prepare is not None:
                prepare(job_id)
            phase = "command_resolution"
            actual_command = command() if callable(command) else command
            with JOBS_LOCK:
                JOBS[job_id].update(stage=starting_stage, message=starting_message, percent=100)
        except BaseException as error:
            with JOBS_LOCK:
                JOBS[job_id].update(
                    status="FAILED",
                    stage="failed",
                    message=_prestart_failure_message(phase),
                    error=str(error),
                    returncode=None,
                )
            LOGGER.exception(
                "job failed before process start",
                extra={
                    "event": "dashboard.job.failed",
                    "job_id": job_id,
                    "result": "failed",
                    "params": {"kind": kind, "phase": phase},
                },
            )
            return
        if actual_command is None:
            with JOBS_LOCK:
                JOBS[job_id].update(
                    status="COMPLETE",
                    stage="complete",
                    message=starting_message,
                    returncode=0,
                )
            LOGGER.info(
                "job completed",
                extra={"event": "dashboard.job.completed", "job_id": job_id, "result": "success", "params": {"kind": kind, "returncode": 0}},
            )
            return
        STATE.mkdir(parents=True, exist_ok=True)
        try:
            process = subprocess.Popen(
                actual_command,
                cwd=str(STATE),
                env=_plugin_environment(),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                # Nautilus logs are UTF-8 with ANSI. Chinese Windows defaults to GBK.
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except Exception as error:
            with JOBS_LOCK:
                JOBS[job_id].update(
                    status="FAILED",
                    stage="failed",
                    message=_prestart_failure_message("process_start"),
                    error=str(error) or "Process could not start",
                    returncode=None,
                )
            LOGGER.exception(
                "job process could not start",
                extra={
                    "event": "dashboard.job.failed",
                    "job_id": job_id,
                    "result": "failed",
                    "params": {"kind": kind, "phase": "process_start"},
                },
            )
            return
        with JOBS_LOCK:
            JOBS[job_id]["_process"] = process
        lines: list[str] = []
        assert process.stdout is not None
        for line in process.stdout:
            cleaned = _strip_ansi(line)
            lines.append(cleaned)
            with JOBS_LOCK:
                JOBS[job_id]["output"] = "".join(lines)[-12000:]
        process.stdout.close()
        returncode = process.wait()
        output = "".join(lines)
        run_id = _job_run_id(output)
        with JOBS_LOCK:
            stopping = JOBS[job_id].get("status") == "STOPPING"
            JOBS[job_id].update(
                status="STOPPED" if stopping else ("COMPLETE" if returncode == 0 else "FAILED"),
                stage="stopped" if stopping else ("complete" if returncode == 0 else "failed"),
                message=JOBS[job_id].get("message") if returncode == 0 or stopping else _execution_failure_message(kind),
                output=output[-12000:],
                run_id=run_id,
                returncode=returncode,
                error=None if returncode == 0 or stopping else _job_error(output, returncode),
            )
            JOBS[job_id].pop("_process", None)
        LOGGER.log(
            logging.INFO if returncode == 0 else logging.ERROR,
            "job completed",
            extra={"event": "dashboard.job.completed" if returncode == 0 else "dashboard.job.failed", "job_id": job_id, "run_id": run_id, "result": "success" if returncode == 0 else "failed", "params": {"kind": kind, "returncode": returncode}},
        )

    def worker() -> None:
        try:
            run_process()
        except Exception as exc:
            with JOBS_LOCK:
                JOBS[job_id].update(status="FAILED", returncode=None, error=str(exc) or "Process could not start")
                JOBS[job_id].pop("_process", None)
            LOGGER.exception(
                "job failed",
                extra={
                    "event": "dashboard.job.failed",
                    "job_id": job_id,
                    "result": "failed",
                    "params": {"kind": kind},
                },
            )

    Thread(target=worker, name=f"edgepilot-{job_id}", daemon=True).start()
    return job_id


def _safe_run_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", value):
        raise ValueError("invalid run id")
    return value


def _safe_directory(parent: Path, name: str) -> Path:
    return safe_directory(parent, name)


def _safe_config_name(value: str) -> str:
    return safe_config_name(value)


def _delete_strategy(name: str) -> None:
    from edgepilot.marketplace import strategy_operation_lock
    with strategy_operation_lock(name):
        path = _safe_directory(strategies_state_root(), name)
        if not path.exists():
            raise FileNotFoundError(name)
        runs_path = path / "runs"
        active = running_runs(runs_path) if runs_path.exists() else {}
        if active:
            raise ValueError("stop active runs before removing this strategy")
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()


def _delete_catalog_dataset(kind: str, name: str) -> None:
    kind_path = _safe_directory(CATALOG, kind)
    path = _safe_directory(kind_path, name)
    if not path.exists() or not path.is_dir():
        raise FileNotFoundError(name)
    shutil.rmtree(path)
    if kind_path.exists() and not any(kind_path.iterdir()):
        kind_path.rmdir()


def _delete_run(run_id: str) -> None:
    run_id = _safe_run_id(run_id)
    path = find_run_directory(run_id)
    if run_id in running_runs(path.parent):
        raise ValueError("stop the active run before deleting it")
    shutil.rmtree(path)


def _emergency_stop_run(run_id: str) -> None:
    """Request strategy cancellation and flattening through Nautilus on_stop."""
    run_id = _safe_run_id(run_id)
    runs_path = find_run_directory(run_id).parent
    if run_id not in running_runs(runs_path):
        raise ValueError("run is not active")
    request_emergency_stop(runs_path, run_id)


def _run_detail(run_id: str) -> dict[str, Any]:
    run_id = _safe_run_id(run_id)
    directory = find_run_directory(run_id)
    record = _read_json(directory / "run.json", None)
    if record is None:
        raise FileNotFoundError(run_id)
    record["run_id"] = run_id
    active = running_runs(directory.parent)
    record["status"] = run_status(directory.parent, run_id, record.get("mode"), active=active)
    execution = load_execution(directory.parent, run_id)
    if execution:
        record["execution"] = execution
    record["timeseries"] = _read_json(directory / "timeseries.json", [])
    record["market_timeseries"] = _market_timeseries(record, directory)
    record["positions"] = _csv_rows(directory / "positions.csv")
    record["fills"] = _csv_rows(directory / "fills.csv")
    record["artifacts"] = {
        "png": (directory / "backtest.png").exists(),
        "directory": str(directory),
    }
    return record


def _runtime_detail(run_id: str) -> dict[str, Any]:
    """Return the latest read-only native report published by a live node."""
    run_id = _safe_run_id(run_id)
    directory = find_run_directory(run_id)
    runtime = _read_json(directory / "runtime.json", {})
    runtime["run_id"] = run_id
    record = _read_json(directory / "run.json", {})
    runtime["status"] = run_status(directory.parent, run_id, record.get("mode"))
    execution = load_execution(directory.parent, run_id)
    if execution:
        runtime["execution"] = execution
    return runtime


def _market_timeseries(record: dict[str, Any], directory: Path) -> dict[str, Any]:
    del record
    return _read_json(directory / "market_timeseries.json", {"markets": []})


def _csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _runtime_prepare(payload: dict[str, Any]) -> Callable[[str], None] | None:
    """Return the one shared delayed-runtime preparation used by Dashboard jobs."""
    from edgepilot.runtime import runtime_status

    if runtime_status().get("installed"):
        return None
    if payload.get("confirm_runtime") is not True:
        raise ValueError("RUNTIME_CONFIRMATION_REQUIRED: confirm the locked EdgePilot Live runtime download")

    def prepare(job_id: str) -> None:
        from edgepilot.runtime import install_runtime

        with RUNTIME_INSTALL_LOCK:
            # Another Dashboard job may have installed the runtime while this
            # job was queued. Never download or activate a second candidate.
            if runtime_status().get("installed"):
                return

            def progress(stage: str, message: str, downloaded: int | None, total: int | None) -> None:
                with JOBS_LOCK:
                    percent = round(downloaded * 100 / total, 1) if downloaded is not None and total else None
                    JOBS[job_id].update(stage=stage, message=message, downloaded_bytes=downloaded or 0, total_bytes=total, percent=percent)

            install_runtime(progress)

    return prepare


def _start_runtime_install(payload: dict[str, Any]) -> str:
    if set(payload) != {"confirm_runtime"} or payload.get("confirm_runtime") is not True:
        raise ValueError("runtime installation requires only confirm_runtime=true")
    return _start_job(
        kind="runtime",
        command=None,
        prepare=_runtime_prepare(payload),
        starting_stage="complete",
        starting_message="运行环境安装完成",
    )


def _start_backtest(payload: dict[str, Any]) -> str:
    if "config" in payload:
        raise ValueError("backtests only accept saved configurations; remove config and provide preset")
    if "days" in payload:
        raise ValueError("backtests load days from the saved preset")
    strategy_name = str(payload.get("strategy", "")).strip()
    if not strategy_name:
        raise ValueError("strategy is required")
    preset = _safe_config_name(str(payload.get("preset", "")).strip())
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,79}", strategy_name):
        raise ValueError("invalid strategy")
    from edgepilot.runtime import active_runtime_python
    allowed = {"strategy", "preset", "confirm_runtime"} if "confirm_runtime" in payload else {"strategy", "preset"}
    if set(payload) != allowed:
        raise ValueError("backtests accept strategy, preset, and optional confirm_runtime")
    prepare = _runtime_prepare(payload)

    def command() -> list[str]:
        prepared = _runtime_dashboard_call("prepare_backtest", {
            "strategy": strategy_name,
            "preset": preset,
        })
        return [str(active_runtime_python()), "-m", "edgepilot.cli", "backtest", prepared["strategy"],
                "--preset", prepared["preset"], "--start", prepared["start"], "--end", prepared["end"]]
    return _start_job(
        kind="backtest",
        command=command,
        prepare=prepare,
        starting_stage="starting_backtest",
        starting_message="启动回测",
    )


def _start_data_pull(payload: dict[str, Any]) -> str:
    from edgepilot.runtime import active_runtime_python

    venue = str(payload.get("venue", "")).strip().upper()
    instrument = str(payload.get("instrument", "")).strip()
    data_type = str(payload.get("data_type", "bars"))
    if not venue or not instrument:
        raise ValueError("venue and instrument are required")
    if data_type not in {"bars", "trades", "quotes", "order-book-depth", "order-book-deltas"}:
        raise ValueError("unsupported data type")
    arguments = ["-m", "edgepilot.cli", "data", "pull", "--venue", venue, "--instrument", instrument, "--data-type", data_type]
    if data_type == "bars":
        arguments.extend(["--bar-type", str(payload.get("bar_type") or "1-HOUR-LAST-EXTERNAL")])
    start_text = str(payload.get("start", "")).strip()
    end_text = str(payload.get("end", "")).strip()
    if start_text or end_text:
        if not start_text or not end_text:
            raise ValueError("start and end must be provided together")
        start = parse_time(start_text); end = parse_time(end_text)
        if start >= end:
            raise ValueError("start must be earlier than end")
        arguments.extend(["--start", start.isoformat(), "--end", end.isoformat()])
    else:
        days = int(payload.get("days", 365))
        if not 1 <= days <= 5000:
            raise ValueError("days must be between 1 and 5000")
        arguments.extend(["--days", str(days)])
    adapter_options = [str(option) for option in payload.get("adapter_set", [])]
    has_account_type = any(option.split("=", 1)[0].strip() == "account_type" for option in adapter_options)
    if venue == "BINANCE" and instrument.endswith("-PERP.BINANCE") and not has_account_type:
        # The dashboard knows this is a USDⓈ-M perpetual from its native ID.
        # Direct bar downloads need the market family, but not API credentials.
        adapter_options.append("account_type=USDT_FUTURES")
    for option in adapter_options:
        arguments.extend(["--adapter-set", str(option)])

    prepare = _runtime_prepare(payload)

    def command() -> list[str]:
        return [str(active_runtime_python()), *arguments]

    return _start_job(
        kind="data",
        command=command,
        prepare=prepare,
        starting_stage="starting_data",
        starting_message="启动数据下载",
    )


def _start_trading(payload: dict[str, Any]) -> str:
    from edgepilot.runtime import active_runtime_python

    if "config" in payload:
        raise ValueError("trading only accepts saved configurations; remove config and provide preset")
    mode = str(payload.get("mode", ""))
    if mode not in {"paper", "demo", "live"}:
        raise ValueError("mode must be paper, demo, or live")
    if mode == "live" and payload.get("confirm_live") is not True:
        raise ValueError("live trading requires explicit confirmation")
    run_id = payload.get("run_id")
    safe_run_id: str | None = None
    strategy_name: str | None = None
    preset: str | None = None
    if run_id:
        safe_run_id = _safe_run_id(str(run_id))
        try:
            find_run_directory(safe_run_id)
        except FileNotFoundError as exc:
            raise ValueError(f"unknown run: {safe_run_id}") from exc
    else:
        strategy_name = str(payload.get("strategy", "")).strip()
        preset_value = str(payload.get("preset", "")).strip()
        if not strategy_name or not preset_value:
            raise ValueError("a saved run or strategy configuration is required")
        preset = _safe_config_name(preset_value)
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,79}", strategy_name):
            raise ValueError("invalid strategy")

    prepare = _runtime_prepare(payload)

    def command() -> list[str]:
        if safe_run_id is not None:
            arguments = ["-m", "edgepilot.cli", mode, "--run", safe_run_id]
        else:
            assert strategy_name is not None and preset is not None
            arguments = ["-m", "edgepilot.cli", mode, "--strategy", strategy_name, "--preset", preset]
        if mode == "live":
            arguments.append("--confirm-live")
        return [str(active_runtime_python()), *arguments]

    return _start_job(
        kind=mode,
        command=command,
        prepare=prepare,
        starting_stage="starting_trading",
        starting_message="启动交易",
    )


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "EdgePilotDashboard/1.0"

    def log_message(self, _format: str, *args: Any) -> None:
        LOGGER.info(
            "dashboard request",
            extra={
                "event": "dashboard.http.completed",
                "params": {"method": self.command, "path": urlparse(self.path).path, "status": args[1] if len(args) > 1 else None},
            },
        )

    def _origin(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def _valid_host(self) -> bool:
        return self.headers.get("Host") == self._origin().removeprefix("http://")

    def _valid_write(self) -> bool:
        expected = getattr(self.server, "edgepilot_csrf", "")
        return self._valid_host() and self.headers.get("Origin") == self._origin() and secrets.compare_digest(self.headers.get("X-EdgePilot-CSRF", ""), expected)

    def _auth_required(self) -> None:
        _error(self, "AUTH_REQUIRED", "Sign in to use EdgePilot.", 401, login={"action": "open_browser"})

    def _require_business_auth(self) -> bool:
        state = auth.status()
        if state.get("authenticated"):
            return True
        reason = str(state.get("reason", "AUTH_SERVICE_UNAVAILABLE"))
        if reason in {"NO_CREDENTIALS", "CREDENTIALS_INVALID", "REFRESH_REJECTED"}:
            self._auth_required()
        elif reason == "ACCOUNT_DISABLED":
            _error(self, reason, "the account is disabled", 403)
        else:
            code = reason if reason in {"CREDENTIAL_STORE_ERROR", "AUTH_SERVICE_UNAVAILABLE"} else "AUTH_SERVICE_UNAVAILABLE"
            LOGGER.warning("dashboard authentication unavailable", extra={"event": "dashboard.auth.unavailable", "result": "failed",
                           "params": {"method": self.command, "path": urlparse(self.path).path, "code": code}})
            _error(self, code, "authentication is temporarily unavailable", 503, retryable=True)
        return False

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
            if parsed.path.startswith("/api/") and not self._valid_host():
                return _error(self, "INVALID_HOST", "invalid Host header", 400)
            if parsed.path == "/":
                return self._asset("app/index.html", "text/html; charset=utf-8")
            if parsed.path.startswith("/assets/"):
                return self._asset(parsed.path.removeprefix("/assets/"), None)
            if parsed.path == "/api/config":
                return _json(self, _dashboard_config(getattr(self.server, "edgepilot_language", None)))
            if parsed.path == "/api/bootstrap":
                return _json(self, {"csrf_token": getattr(self.server, "edgepilot_csrf", "")})
            if parsed.path == "/api/health":
                identity = getattr(self.server, "edgepilot_identity", None)
                if isinstance(identity, dict):
                    if not secrets.compare_digest(self.headers.get("X-EdgePilot-Instance", ""), str(identity.get("instance_nonce", ""))):
                        return _error(self, "INSTANCE_MISMATCH", "Dashboard instance does not match", 403)
                    return _json(self, identity)
                return _json(self, {"ok": True})
            login_match = re.fullmatch(r"/api/auth/login/([A-Za-z0-9_-]{20,})/status", parsed.path)
            if login_match:
                return _json(self, auth.login_status(login_match.group(1)))
            if parsed.path == "/api/runs/active":
                return _json(self, {"runs": [{"run_id": row["run_id"], "strategy": row.get("strategy", {}).get("name", ""), "mode": row.get("mode", "")}
                    for row in _run_records() if row.get("status") == "RUNNING"]})
            if parsed.path.startswith("/api/") and not self._require_business_auth():
                return
            if parsed.path == "/api/marketplace/strategies":
                from edgepilot.marketplace import search
                query = parse_qs(parsed.query)
                page = int(query.get("page", ["1"])[0])
                page_size = int(query.get("page_size", ["30"])[0])
                return _json(self, search(query=query.get("q", [""])[0], risk_profile=query.get("risk_profile", [""])[0],
                    min_capacity_usd=float(query["min_capacity_usd"][0]) if query.get("min_capacity_usd") else None,
                    sort=query.get("sort", ["published"])[0], locale=query.get("locale", [""])[0], page=page, page_size=page_size))
            if parsed.path == "/api/marketplace/history":
                from edgepilot.marketplace import installation_history
                return _json(self, installation_history())
            versions_match = re.fullmatch(r"/api/marketplace/strategies/([^/]+)/versions", parsed.path)
            detail_match = re.fullmatch(r"/api/marketplace/strategies/([^/]+)/([^/]+)", parsed.path)
            if versions_match:
                from edgepilot.marketplace import versions
                return _json(self, versions(versions_match.group(1)))
            if detail_match:
                from edgepilot.marketplace import inspect
                query = parse_qs(parsed.query)
                return _json(self, inspect(detail_match.group(1), detail_match.group(2), locale=query.get("locale", [""])[0]))
            if parsed.path == "/api/strategies":
                query = parse_qs(parsed.query)
                locale = normalize_supported_locale(query.get("locale", ["en"])[0]) or "en"
                return _json(self, _strategy_records(locale))
            strategy_config_match = re.fullmatch(r"/api/strategies/([^/]+)/configs/([^/]+)", parsed.path)
            if strategy_config_match:
                return _json(self, _runtime_dashboard_call("strategy_config", {
                    "strategy": strategy_config_match.group(1),
                    "name": strategy_config_match.group(2),
                }))
            if parsed.path.startswith("/api/strategies/"):
                return _json(self, _runtime_dashboard_call("strategy_detail", {
                    "strategy": parsed.path.rsplit("/", 1)[-1],
                }))
            if parsed.path == "/api/runs":
                return _json(self, _run_records())
            if parsed.path == "/api/catalog":
                return _json(self, _catalog_records())
            if parsed.path == "/api/credentials":
                return _json(self, _runtime_dashboard_call("credentials", {}))
            if parsed.path == "/api/jobs":
                with JOBS_LOCK:
                    return _json(self, [_job_view(job) for job in JOBS.values()])
            if parsed.path.startswith("/api/jobs/"):
                job_id = parsed.path.rsplit("/", 1)[-1]
                with JOBS_LOCK:
                    job = JOBS.get(job_id)
                if job is None:
                    return _error(self, "JOB_NOT_FOUND", "job not found", 404)
                return _json(self, _job_view(job))
            if parsed.path.startswith("/api/runs/") and parsed.path.endswith("/chart"):
                run_id = parsed.path.split("/")[3]
                return self._run_file(run_id, "backtest.png", "image/png")
            if parsed.path.startswith("/api/runs/") and parsed.path.endswith("/runtime"):
                run_id = parsed.path.split("/")[3]
                return _json(self, _runtime_detail(run_id))
            if parsed.path.startswith("/api/runs/"):
                return _json(self, _run_detail(parsed.path.rsplit("/", 1)[-1]))
            return _error(self, "NOT_FOUND", "not found", 404)
        except FileNotFoundError:
            _error(self, "NOT_FOUND", "not found", 404)
        except (ValueError, TypeError) as exc:
            _error(self, "VALIDATION_FAILED", str(exc), 400)
        except Exception as exc:  # keep the dashboard usable while surfacing errors
            LOGGER.exception("dashboard request failed", extra={"event": "dashboard.http.failed", "result": "failed", "params": {"method": self.command, "path": parsed.path}})
            _error(self, "INTERNAL_ERROR", str(exc), 500)

    def do_POST(self) -> None:  # noqa: N802
        try:
            parsed = urlparse(self.path)
            if not self._valid_write():
                return _error(self, "CSRF_REJECTED", "invalid Host, Origin, or CSRF token", 403)
            length = int(self.headers.get("Content-Length", "0"))
            if length > 16 * 1024:
                return _error(self, "REQUEST_TOO_LARGE", "request body exceeds 16 KiB", 413)
            if parsed.path == "/api/auth/status":
                state = auth.status()
                reason = str(state.get("reason", ""))
                if not state.get("authenticated") and reason in {"CREDENTIAL_STORE_ERROR", "AUTH_SERVICE_UNAVAILABLE"}:
                    return _error(self, reason, "authentication is temporarily unavailable", 503, retryable=True)
                return _json(self, state)
            if parsed.path == "/api/auth/login":
                started = auth.start_login(open_browser=False)
                public = {key: started[key] for key in (
                    "login_id", "verification_uri", "verification_uri_complete", "user_code", "expires_in"
                )}
                _run_login_worker(public)
                return _json(self, public, 201)
            if parsed.path == "/api/auth/logout":
                payload = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(payload, dict) or not set(payload).issubset({"local_only", "all"}):
                    raise ValueError("logout accepts only local_only and all")
                return _json(self, auth.logout(all_devices=payload.get("all") is True, local_only=payload.get("local_only") is True))
            if parsed.path == "/api/marketplace/install":
                payload = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(payload, dict) or set(payload) != {"slug", "version"}:
                    raise ValueError("install only accepts slug and version")
                from edgepilot.marketplace import download_and_install, preflight_install
                preflight_install(str(payload["slug"]), str(payload["version"]))
                if not self._require_business_auth():
                    return
                return _json(self, download_and_install(str(payload["slug"]), str(payload["version"])), 201)
            if re.fullmatch(r"/api/runs/[^/]+/emergency-stop", parsed.path):
                pass
            elif not self._require_business_auth():
                return
            payload = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(payload, dict):
                raise TypeError("request body must be a JSON object")
            if self.path == "/api/marketplace/recommendations":
                from edgepilot.marketplace import recommend
                return _json(self, recommend(payload))
            if self.path == "/api/backtests":
                return _json(self, {"job_id": _start_backtest(payload)}, 202)
            strategy_config_match = re.fullmatch(r"/api/strategies/([^/]+)/configs/([^/]+)", self.path)
            if strategy_config_match:
                if set(payload) != {"config"}:
                    raise ValueError("configuration request body must contain only config")
                values = _runtime_dashboard_call("create_strategy_config", {
                    "strategy": strategy_config_match.group(1),
                    "name": strategy_config_match.group(2),
                    "config": payload.get("config"),
                })
                return _json(self, {"saved": strategy_config_match.group(2), "config": values}, 201)
            if self.path == "/api/data/pull":
                return _json(self, {"job_id": _start_data_pull(payload)}, 202)
            if self.path == "/api/credentials":
                if "confirm_runtime" in payload:
                    return _json(self, {"job_id": _start_runtime_install(payload)}, 202)
                return _json(self, _runtime_dashboard_call("save_credentials", payload), 201)
            if self.path == "/api/trading":
                return _json(self, {"job_id": _start_trading(payload)}, 202)
            if self.path.startswith("/api/jobs/") and self.path.endswith("/stop"):
                job_id = self.path.split("/")[3]
                with JOBS_LOCK:
                    job = JOBS.get(job_id)
                    process = job.get("_process") if job else None
                    if job is None:
                        return _error(self, "JOB_NOT_FOUND", "job not found", 404)
                    if process is None or process.poll() is not None:
                        return _error(self, "JOB_NOT_RUNNING", "job is not running", 409)
                    job["status"] = "STOPPING"
                    process.terminate()
                return _json(self, {"job_id": job_id, "status": "STOPPING"}, 202)
            if self.path.startswith("/api/runs/") and self.path.endswith("/emergency-stop"):
                _emergency_stop_run(self.path.split("/")[3])
                return _json(self, {"status": "EMERGENCY_STOPPING"}, 202)
            return _error(self, "NOT_FOUND", "not found", 404)
        except ConfigConflictError as exc:
            return _error(self, "CONFLICT", str(exc), 409)
        except FileNotFoundError:
            return _error(self, "NOT_FOUND", "not found", 404)
        except ModuleNotFoundError as exc:
            return _error(self, "VALIDATION_FAILED", str(exc), 400)
        except MarketplaceRequestError as exc:
            LOGGER.warning("dashboard Marketplace request failed", extra={"event": "dashboard.marketplace.failed", "result": "failed", "params": {"method": "POST", "path": self.path, "code": exc.code}})
            return _marketplace_request_error(self, exc)
        except auth.AuthError as exc:
            LOGGER.warning("dashboard authentication request failed", extra={"event": "dashboard.auth.failed", "result": "failed", "params": {"method": "POST", "path": self.path, "code": str(exc)}})
            return _error(self, "AUTH_SERVICE_UNAVAILABLE", "authentication service is unavailable", 502)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            return _error(self, "VALIDATION_FAILED", str(exc), 400)
        except OSError as exc:
            LOGGER.exception("dashboard write failed", extra={"event": "dashboard.http.write_failed", "result": "failed", "params": {"method": "POST", "path": self.path}})
            return _error(self, "INTERNAL_ERROR", str(exc), 500)

    def do_PUT(self) -> None:  # noqa: N802
        try:
            if not self._valid_write():
                return _error(self, "CSRF_REJECTED", "invalid Host, Origin, or CSRF token", 403)
            if not self._require_business_auth():
                return
            length = int(self.headers.get("Content-Length", "0"))
            if length > 16 * 1024:
                return _error(self, "REQUEST_TOO_LARGE", "request body exceeds 16 KiB", 413)
            payload = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(payload, dict):
                raise TypeError("request body must be a JSON object")
            strategy_config_match = re.fullmatch(r"/api/strategies/([^/]+)/configs/([^/]+)", self.path)
            if not strategy_config_match:
                return _error(self, "NOT_FOUND", "not found", 404)
            if set(payload) != {"config"}:
                raise ValueError("configuration request body must contain only config")
            values = _runtime_dashboard_call("update_strategy_config", {
                "strategy": strategy_config_match.group(1),
                "name": strategy_config_match.group(2),
                "config": payload.get("config"),
            })
            return _json(self, {"saved": strategy_config_match.group(2), "config": values})
        except ConfigConflictError as exc:
            return _error(self, "CONFLICT", str(exc), 409)
        except FileNotFoundError:
            return _error(self, "NOT_FOUND", "not found", 404)
        except ModuleNotFoundError:
            return _error(self, "NOT_FOUND", "not found", 404)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            return _error(self, "VALIDATION_FAILED", str(exc), 400)
        except OSError as exc:
            LOGGER.exception("dashboard write failed", extra={"event": "dashboard.http.write_failed", "result": "failed", "params": {"method": "PUT", "path": self.path}})
            return _error(self, "INTERNAL_ERROR", str(exc), 500)

    def do_DELETE(self) -> None:  # noqa: N802
        try:
            if not self._valid_write():
                return _error(self, "CSRF_REJECTED", "invalid Host, Origin, or CSRF token", 403)
            if not self._require_business_auth():
                return
            if self.headers.get("X-EdgePilot-Confirm") != "delete":
                return _error(self, "CONFIRMATION_REQUIRED", "deletion requires an explicit confirmation", 400)
            parsed = urlparse(self.path)
            parts = [part for part in parsed.path.split("/") if part]
            if len(parts) == 4 and parts[:3] == ["api", "marketplace", "history"]:
                from edgepilot.marketplace import clear_installation_history
                return _json(self, clear_installation_history(parts[3]))
            if len(parts) == 3 and parts[:2] == ["api", "strategies"]:
                _delete_strategy(parts[2])
                return _json(self, {"deleted": parts[2]})
            if len(parts) == 4 and parts[:2] == ["api", "catalog"]:
                _delete_catalog_dataset(parts[2], parts[3])
                return _json(self, {"deleted": parts[3]})
            if len(parts) == 3 and parts[:2] == ["api", "runs"]:
                _delete_run(parts[2])
                return _json(self, {"deleted": parts[2]})
            return _error(self, "NOT_FOUND", "not found", 404)
        except FileNotFoundError:
            return _error(self, "NOT_FOUND", "not found", 404)
        except (ValueError, OSError) as exc:
            return _error(self, "VALIDATION_FAILED", str(exc), 400)

    def _asset(self, relative: str, content_type: str | None) -> None:
        path = (ASSETS / relative).resolve()
        if not path.is_relative_to(ASSETS.resolve()) or not path.exists():
            return _error(self, "NOT_FOUND", "not found", 404)
        body = path.read_bytes()
        self.send_response(200)
        guessed_type, _ = mimetypes.guess_type(path.name)
        self.send_header("Content-Type", content_type or guessed_type or "application/octet-stream")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if path.name == "index.html":
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; connect-src 'self'; img-src 'self' data:; "
                "style-src 'self' 'unsafe-inline'; script-src 'self'; "
                "frame-ancestors 'none'; base-uri 'none'",
            )
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _run_file(self, run_id: str, filename: str, content_type: str) -> None:
        run_id = _safe_run_id(run_id)
        directory = find_run_directory(run_id)
        path = (directory / filename).resolve()
        if path.parent != directory.resolve() or not path.exists():
            return _error(self, "ARTIFACT_NOT_FOUND", "artifact not found", 404)
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve(*, host: str = "127.0.0.1", port: int = 8787, language: str | None = None) -> None:
    """Serve the local dashboard; no network access beyond localhost."""
    if host != "127.0.0.1":
        raise ValueError("Dashboard must bind to 127.0.0.1")
    server = ThreadingHTTPServer((host, port), DashboardHandler)
    server.edgepilot_language = normalize_supported_locale(language)  # type: ignore[attr-defined]
    server.edgepilot_csrf = secrets.token_urlsafe(32)  # type: ignore[attr-defined]
    pending_login = auth.resume_login()
    if pending_login is not None:
        _run_login_worker(pending_login)
    LOGGER.info("dashboard started", extra={"event": "dashboard.started", "params": {"host": host, "port": port}})
    print(f"EdgePilot dashboard: http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        LOGGER.info("dashboard stopped", extra={"event": "dashboard.stopped", "result": "success"})
