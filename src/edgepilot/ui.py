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
import tempfile
import secrets
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock, Thread
from typing import Any
from urllib.parse import parse_qs, urlparse

from edgepilot.discovery import discover_execution_adapters, resolve_adapter, resolve_strategy, strategies_root, strategy_names
from edgepilot.environment import credential_requirements, load_env
from edgepilot.marketplace import MarketplaceRequestError, install_package
from edgepilot.models import MarketRequest
from edgepilot.catalog import parse_time
from edgepilot.paths import state_root
from edgepilot.paths import find_run_directory
from edgepilot.paths import iter_run_directories
from edgepilot.paths import strategy_runs_path
from edgepilot.presets import load_preset, preset_backtest_values, preset_markets, preset_names, preset_strategy_values, preset_venues, resolve_strategy_parameters
from edgepilot.trading import request_emergency_stop, running_runs
from edgepilot.app_logging import configure_logging
from edgepilot.locale import SUPPORTED_LANGUAGES, normalize_supported_locale
from edgepilot import auth


ASSETS = Path(__file__).with_name("ui_assets")
STATE = state_root()
CATALOG_ROOT = STATE / "catalog"
CATALOG = CATALOG_ROOT / "data"
JOBS: dict[str, dict[str, Any]] = {}
JOBS_LOCK = Lock()
LOCAL_LOGINS: dict[str, dict[str, Any]] = {}
LOCAL_LOGINS_LOCK = Lock()


def _prune_local_logins(now: float | None = None) -> None:
    current = time.time() if now is None else now
    with LOCAL_LOGINS_LOCK:
        expired = [key for key, item in LOCAL_LOGINS.items() if current - item["created_at"] > 660 or
                   item.get("completed_at") and current - item["completed_at"] > 60]
        for key in expired:
            LOCAL_LOGINS.pop(key, None)

# The dashboard process launches child CLI commands, so it needs the same
# local-only environment values as the CLI itself. Values never leave this
# process or appear in an API response.
load_env(STATE / ".env")
LOGGER = configure_logging()


class ConfigConflictError(ValueError):
    """Raised when a user configuration would overwrite protected state."""


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
    "SERVICE_UNAVAILABLE": (503, "the recommendation service is unavailable"),
    "AUTH_SERVICE_UNAVAILABLE": (503, "the authentication service is unavailable"),
}


def _marketplace_request_error(handler: BaseHTTPRequestHandler, exc: MarketplaceRequestError) -> None:
    status, message = _MARKETPLACE_ERROR_RESPONSES.get(exc.code, (500, "the recommendation request failed"))
    code = exc.code if exc.code in _MARKETPLACE_ERROR_RESPONSES else "INTERNAL_ERROR"
    _error(handler, code, message, status, retryable=exc.retryable)


def _dashboard_config(language: str | None) -> dict[str, Any]:
    return {"language": normalize_supported_locale(language), "supported_languages": list(SUPPORTED_LANGUAGES)}


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _run_records() -> list[dict[str, Any]]:
    records = []
    for path in sorted(iter_run_directories(), reverse=True):
        record = _read_json(path / "run.json", {})
        record["run_id"] = path.name
        active = running_runs(path.parent)
        record["status"] = "RUNNING" if path.name in active else ("COMPLETE" if record.get("mode") == "backtest" else "STOPPED")
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
    return [
        ({
            "name": name,
            "presets": preset_names(resolve_strategy(name)),
            "source": "marketplace" if (strategies_root() / name / ".marketplace.json").exists() else "local",
            "marketplace": _read_json(strategies_root() / name / ".marketplace.json", None),
        } | _strategy_content(strategies_root() / name, name, locale))
        for name in strategy_names()
    ]


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


def _credential_venues() -> list[str]:
    """Return native venues the dashboard can configure without exposing keys."""
    venues = []
    for adapter in discover_execution_adapters():
        try:
            if any(credential_requirements(adapter, mode) for mode in ("demo", "live")):
                venues.append(adapter.name)
        except (ImportError, ValueError, TypeError, AttributeError) as exc:
            LOGGER.debug(
                "credential adapter skipped",
                extra={"event": "credentials.adapter.skipped", "params": {"adapter": adapter.name, "error": type(exc).__name__}},
            )
    return sorted(set(venues))


def _credential_records() -> list[dict[str, Any]]:
    records = []
    for venue in _credential_venues():
        try:
            adapter = resolve_adapter(venue)
        except (ImportError, ValueError):
            continue
        records.append({
            "venue": adapter.name,
            "modes": {
                mode: credential_requirements(adapter, mode)
                for mode in ("paper", "demo", "live")
            },
        })
    return records


def _save_credentials(payload: dict[str, Any]) -> dict[str, Any]:
    venue = str(payload.get("venue", "")).strip().upper()
    mode = str(payload.get("mode", "")).strip().lower()
    submitted = payload.get("values", {})
    if venue not in _credential_venues() or mode not in {"paper", "demo", "live"}:
        raise ValueError("unknown venue or credential mode")
    if not isinstance(submitted, dict):
        raise ValueError("credential values must be an object")
    adapter = resolve_adapter(venue)
    requirements = credential_requirements(adapter, mode)
    permitted = {item["field"]: item["environment_variable"] for item in requirements}
    updates: dict[str, str] = {}
    for field, value in submitted.items():
        if field not in permitted:
            raise ValueError(f"unsupported credential field: {field}")
        if not isinstance(value, str):
            raise ValueError("credential values must be text")
        if "\n" in value or "\r" in value:
            raise ValueError("credential values cannot contain new lines")
        if value:
            updates[permitted[field]] = value
    if not updates:
        raise ValueError("enter at least one credential value to save")
    env_path = STATE / ".env"
    existing = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    retained = [
        line for line in existing
        if not any(line.lstrip().removeprefix("export ").lstrip().startswith(f"{name}=") for name in updates)
    ]
    retained.extend(f"{name}={value}" for name, value in updates.items())
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text("\n".join(retained).rstrip() + "\n", encoding="utf-8")
    try:
        env_path.chmod(0o600)
    except OSError:
        pass
    os.environ.update(updates)
    return {"venue": adapter.name, "mode": mode, "saved_fields": sorted(submitted)}


def _job_view(job: dict[str, Any]) -> dict[str, Any]:
    """Return only serializable, safe job fields to the browser."""
    return {key: value for key, value in job.items() if not key.startswith("_")}


_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[@-Z\\-_]")


def _strip_ansi(text: str) -> str:
    """Remove terminal color and control sequences from subprocess output."""
    return _ANSI_ESCAPE.sub("", text)


def _job_error(output: str, returncode: int) -> str:
    """Extract the CLI's user-facing error without exposing arbitrary logs."""
    for line in reversed(_strip_ansi(output).splitlines()):
        message = line.strip()
        if message.lower().startswith("error:"):
            detail = message.split(":", 1)[1].strip()
            if detail:
                return detail
    return f"Process exited with code {returncode}"


def _start_job(*, kind: str, command: list[str]) -> str:
    job_id = f"job-{__import__('datetime').datetime.now().strftime('%Y%m%d%H%M%S%f')}"
    with JOBS_LOCK:
        JOBS[job_id] = {
            "job_id": job_id,
            "kind": kind,
            "status": "QUEUED",
            "output": "",
            "run_id": None,
            "error": None,
        }

    def run_process() -> None:
        LOGGER.info("job started", extra={"event": "dashboard.job.started", "job_id": job_id, "params": {"kind": kind, "command": command[:4]}})
        with JOBS_LOCK:
            JOBS[job_id]["status"] = "RUNNING"
        STATE.mkdir(parents=True, exist_ok=True)
        process = subprocess.Popen(
            command,
            cwd=str(STATE),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            # Nautilus logs are UTF-8 with ANSI. Chinese Windows defaults to GBK.
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
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
        match = re.search(r'"run_id"\s*:\s*"([^"]+)"', output)
        with JOBS_LOCK:
            stopping = JOBS[job_id].get("status") == "STOPPING"
            JOBS[job_id].update(
                status="STOPPED" if stopping else ("COMPLETE" if returncode == 0 else "FAILED"),
                output=output[-12000:],
                run_id=match.group(1) if match else None,
                returncode=returncode,
                error=None if returncode == 0 or stopping else _job_error(output, returncode),
            )
            JOBS[job_id].pop("_process", None)
        LOGGER.log(
            logging.INFO if returncode == 0 else logging.ERROR,
            "job completed",
            extra={"event": "dashboard.job.completed" if returncode == 0 else "dashboard.job.failed", "job_id": job_id, "run_id": match.group(1) if match else None, "result": "success" if returncode == 0 else "failed", "params": {"kind": kind, "returncode": returncode}},
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
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,160}", name):
        raise ValueError("invalid local item name")
    path = (parent / name).resolve()
    if path.parent != parent.resolve():
        raise ValueError("invalid local path")
    return path


def _safe_config_name(value: str) -> str:
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,79}", value):
        raise ValueError("configuration names use lowercase letters, numbers, underscores, and hyphens")
    return value


def _reject_secret_values(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if any(fragment in str(key).lower() for fragment in ("secret", "api_key", "api_secret", "password", "passphrase", "token", "private_key")):
                raise ValueError("credentials belong in the local .env file, not a configuration")
            _reject_secret_values(item)
    elif isinstance(value, list):
        for item in value:
            _reject_secret_values(item)


def _without_none(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _without_none(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [_without_none(item) for item in value]
    return value


def _validated_strategy_config(strategy_name: str, config_name: str, values: Any) -> tuple[Path, dict[str, Any]]:
    strategy = resolve_strategy(strategy_name)
    config_name = _safe_config_name(config_name)
    if config_name == "default":
        raise ConfigConflictError("default is a read-only recommended configuration; save it under a new name")
    if not isinstance(values, dict):
        raise ValueError("configuration must be a JSON object")
    _reject_secret_values(values)
    strategy_values = _without_none(resolve_strategy_parameters(strategy, preset_strategy_values(values)))
    markets = preset_markets(values)
    venues = preset_venues(values)
    backtest = values.get("backtest", {})
    days = backtest.get("days") if isinstance(backtest, dict) else None
    if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 5000:
        raise ValueError("Preset backtest.days must be an integer between 1 and 5000")
    for market in markets:
        for key in ("instrument_id", "bar_type", "venue"):
            if not str(market.get(key, "")).strip():
                raise ValueError(f"Market {key} must not be empty")
        if str(market["venue"]).upper() not in venues:
            raise ValueError(f"Market venue is not configured: {market['venue']}")
    directory = strategies_root() / strategy.name / "configs"
    directory.mkdir(parents=True, exist_ok=True)
    path = _safe_directory(directory, f"{config_name}.json")
    snapshot = {**values, "strategy": strategy_values}
    return path, snapshot


def _config_bytes(values: dict[str, Any]) -> bytes:
    return (json.dumps(values, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _create_strategy_config(strategy_name: str, config_name: str, values: Any) -> dict[str, Any]:
    strategy = resolve_strategy(strategy_name)
    safe_name = _safe_config_name(config_name)
    if safe_name == "default":
        raise ConfigConflictError("default is a read-only recommended configuration; save it under a new name")
    existing = _safe_directory(strategies_root() / strategy.name / "configs", f"{safe_name}.json")
    if existing.exists():
        raise ConfigConflictError(f"configuration already exists: {config_name}")
    path, snapshot = _validated_strategy_config(strategy_name, config_name, values)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(_config_bytes(snapshot))
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            raise ConfigConflictError(f"configuration already exists: {config_name}") from exc
    finally:
        temporary.unlink(missing_ok=True)
    return snapshot


def _update_strategy_config(strategy_name: str, config_name: str, values: Any) -> dict[str, Any]:
    strategy = resolve_strategy(strategy_name)
    safe_name = _safe_config_name(config_name)
    if safe_name == "default":
        raise ConfigConflictError("default is a read-only recommended configuration; save it under a new name")
    path = _safe_directory(strategies_root() / strategy.name / "configs", f"{safe_name}.json")
    if not path.exists():
        raise FileNotFoundError(config_name)
    path, snapshot = _validated_strategy_config(strategy_name, config_name, values)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(_config_bytes(snapshot))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return snapshot


def _delete_strategy(name: str) -> None:
    from edgepilot.marketplace import strategy_operation_lock
    with strategy_operation_lock(name):
        path = _safe_directory(strategies_root(), name)
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
    record["status"] = "RUNNING" if run_id in running_runs(directory.parent) else ("COMPLETE" if record.get("mode") == "backtest" else "STOPPED")
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
    runtime["status"] = "RUNNING" if run_id in running_runs(directory.parent) else "STOPPED"
    return runtime


def _market_timeseries(record: dict[str, Any], directory: Path) -> dict[str, Any]:
    cached = _read_json(directory / "market_timeseries.json", {"markets": []})
    if cached.get("markets"):
        return cached
    period = record.get("period", {})
    if not period.get("start") or not period.get("end"):
        return cached
    try:
        from edgepilot.reporting import _load_market

        start = datetime.fromisoformat(str(period["start"]))
        end = datetime.fromisoformat(str(period["end"]))
        markets = []
        for market in record.get("markets", []):
            bars, _ = _load_market(STATE / "catalog", str(market["bar_type"]), start, end)
            markets.append({
                "instrument_id": str(market["instrument_id"]),
                "series": json.loads(bars[["timestamp", "close"]].to_json(orient="records", date_format="iso")),
            })
        result = {"markets": markets}
        if markets:
            (directory / "market_timeseries.json").write_text(json.dumps(result), encoding="utf-8")
        return result
    except (KeyError, OSError, ValueError, RuntimeError):
        return cached


def _csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _start_backtest(payload: dict[str, Any]) -> str:
    if "config" in payload:
        raise ValueError("backtests only accept saved configurations; remove config and provide preset")
    if "days" in payload:
        raise ValueError("backtests load days from the saved preset")
    strategy_name = str(payload.get("strategy", "")).strip()
    if not strategy_name:
        raise ValueError("strategy is required")
    strategy = resolve_strategy(strategy_name)
    preset = _safe_config_name(str(payload.get("preset", "")).strip())
    if preset not in preset_names(strategy):
        raise ValueError(f"unknown preset: {preset}")
    _, values = load_preset(strategy, preset)
    backtest = preset_backtest_values(values)
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=min(int(backtest.get("days", 365)), 90))
    if not bool(backtest.get("download", True)):
        from edgepilot.backtest import CatalogDataRequiredError, missing_catalog_requirements

        markets = tuple(MarketRequest(
            instrument_id=str(item["instrument_id"]),
            bar_type=str(item["bar_type"]),
            venue=str(item["venue"]).upper(),
            data_type=str(item.get("data_type", "bars")),
        ) for item in preset_markets(values))
        requirements = missing_catalog_requirements(CATALOG_ROOT, markets, start, end)
        if requirements:
            raise CatalogDataRequiredError(CATALOG_ROOT, requirements)
    command = [sys.executable, "-m", "edgepilot.cli", "backtest", strategy.name]
    command.extend(["--preset", preset, "--start", start.isoformat(), "--end", end.isoformat()])
    return _start_job(kind="backtest", command=command)


def _start_data_pull(payload: dict[str, Any]) -> str:
    venue = str(payload.get("venue", "")).strip().upper()
    instrument = str(payload.get("instrument", "")).strip()
    data_type = str(payload.get("data_type", "bars"))
    if not venue or not instrument:
        raise ValueError("venue and instrument are required")
    if data_type not in {"bars", "trades", "quotes", "order-book-depth", "order-book-deltas"}:
        raise ValueError("unsupported data type")
    command = [sys.executable, "-m", "edgepilot.cli", "data", "pull", "--venue", venue, "--instrument", instrument, "--data-type", data_type]
    if data_type == "bars":
        command.extend(["--bar-type", str(payload.get("bar_type") or "1-HOUR-LAST-EXTERNAL")])
    start_text = str(payload.get("start", "")).strip()
    end_text = str(payload.get("end", "")).strip()
    if start_text or end_text:
        if not start_text or not end_text:
            raise ValueError("start and end must be provided together")
        start = parse_time(start_text); end = parse_time(end_text)
        if start >= end:
            raise ValueError("start must be earlier than end")
        command.extend(["--start", start.isoformat(), "--end", end.isoformat()])
    else:
        days = int(payload.get("days", 365))
        if not 1 <= days <= 5000:
            raise ValueError("days must be between 1 and 5000")
        command.extend(["--days", str(days)])
    adapter_options = [str(option) for option in payload.get("adapter_set", [])]
    has_account_type = any(option.split("=", 1)[0].strip() == "account_type" for option in adapter_options)
    if venue == "BINANCE" and instrument.endswith("-PERP.BINANCE") and not has_account_type:
        # The dashboard knows this is a USDⓈ-M perpetual from its native ID.
        # Direct bar downloads need the market family, but not API credentials.
        adapter_options.append("account_type=USDT_FUTURES")
    for option in adapter_options:
        command.extend(["--adapter-set", str(option)])
    return _start_job(kind="data", command=command)


def _start_trading(payload: dict[str, Any]) -> str:
    if "config" in payload:
        raise ValueError("trading only accepts saved configurations; remove config and provide preset")
    mode = str(payload.get("mode", ""))
    if mode not in {"paper", "demo", "live"}:
        raise ValueError("mode must be paper, demo, or live")
    if mode == "live" and payload.get("confirm_live") is not True:
        raise ValueError("live trading requires explicit confirmation")
    run_id = payload.get("run_id")
    if run_id:
        safe_run_id = _safe_run_id(str(run_id))
        try:
            find_run_directory(safe_run_id)
        except FileNotFoundError as exc:
            raise ValueError(f"unknown run: {safe_run_id}") from exc
        command = [sys.executable, "-m", "edgepilot.cli", mode, "--run", safe_run_id]
    else:
        strategy_name = str(payload.get("strategy", "")).strip()
        preset_value = str(payload.get("preset", "")).strip()
        if not strategy_name or not preset_value:
            raise ValueError("a saved run or strategy configuration is required")
        preset = _safe_config_name(preset_value)
        strategy = resolve_strategy(strategy_name)
        if preset not in preset_names(strategy):
            raise ValueError(f"unknown configuration: {preset}")
        command = [sys.executable, "-m", "edgepilot.cli", mode, "--strategy", strategy.name, "--preset", preset]
    if mode == "live":
        command.append("--confirm-live")
    return _start_job(kind=mode, command=command)


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

    def _business_authenticated(self) -> bool:
        return bool(auth.status().get("authenticated"))

    def _auth_required(self) -> None:
        _error(self, "AUTH_REQUIRED", "Sign in to use EdgePilot.", 401, login={"action": "open_browser"})

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
                return _json(self, {"ok": True})
            login_match = re.fullmatch(r"/api/auth/login/([A-Za-z0-9_-]{20,})/status", parsed.path)
            if login_match:
                _prune_local_logins()
                with LOCAL_LOGINS_LOCK:
                    item = LOCAL_LOGINS.get(login_match.group(1))
                return _json(self, item and {key: value for key, value in item.items() if key not in {"device", "created_at", "completed_at"}} or {"status": "expired"})
            if parsed.path == "/api/runs/active":
                return _json(self, {"runs": [{"run_id": row["run_id"], "strategy": row.get("strategy", {}).get("name", ""), "mode": row.get("mode", "")}
                    for row in _run_records() if row.get("status") == "RUNNING"]})
            if parsed.path.startswith("/api/") and not self._business_authenticated():
                return self._auth_required()
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
                strategy = resolve_strategy(strategy_config_match.group(1))
                name, values = load_preset(strategy, _safe_config_name(strategy_config_match.group(2)))
                return _json(self, {"strategy": strategy.name, "name": name, "config": values})
            if parsed.path.startswith("/api/strategies/"):
                strategy = resolve_strategy(parsed.path.rsplit("/", 1)[-1])
                return _json(self, {
                    "name": strategy.name,
                    "strategy_path": strategy.strategy_path,
                    "config_path": strategy.config_path,
                    "presets": preset_names(strategy),
                    "config_schema": strategy.config_cls.json_schema(),
                })
            if parsed.path == "/api/runs":
                return _json(self, _run_records())
            if parsed.path == "/api/catalog":
                return _json(self, _catalog_records())
            if parsed.path == "/api/credentials":
                return _json(self, _credential_records())
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
                return _json(self, auth.status())
            if parsed.path == "/api/auth/login":
                _prune_local_logins()
                with LOCAL_LOGINS_LOCK:
                    existing = next(((key, value) for key, value in LOCAL_LOGINS.items() if value.get("status") == "pending" and time.time() - value["created_at"] < 600), None)
                if existing:
                    return _json(self, {"login_id": existing[0], **{key: value for key, value in existing[1].items() if key not in {"device", "created_at", "status"}}})
                device = auth.start_login(open_browser=False); login_id = secrets.token_urlsafe(24)
                item = {"status": "pending", "created_at": time.time(), "device": device,
                    "verification_uri": device["verification_uri"], "verification_uri_complete": device["verification_uri_complete"],
                    "user_code": device["user_code"], "expires_in": device["expires_in"]}
                with LOCAL_LOGINS_LOCK: LOCAL_LOGINS[login_id] = item
                def poll() -> None:
                    try:
                        result = auth.poll_login(device)
                        state = "authenticated" if result.get("authenticated") else "denied" if result.get("reason") == "ACCESS_DENIED" else "expired"
                        update = {"status": state, **({"credential_storage": result.get("credential_storage")} if state == "authenticated" else {})}
                    except auth.AuthError as exc:
                        LOGGER.warning("dashboard login failed", extra={"event": "dashboard.auth.login_failed", "result": "failed",
                                       "params": {"code": str(exc), "stage": exc.stage or "unknown", **exc.diagnostics}})
                        update = {"status": "failed", "reason": str(exc) if str(exc) in {"AUTH_SERVICE_UNAVAILABLE", "CREDENTIAL_STORE_ERROR"} else "PROTOCOL_ERROR"}
                    with LOCAL_LOGINS_LOCK:
                        if login_id in LOCAL_LOGINS: LOCAL_LOGINS[login_id].update(update, completed_at=time.time())
                Thread(target=poll, daemon=True).start()
                return _json(self, {"login_id": login_id, **{key: value for key, value in item.items() if key not in {"device", "created_at", "status"}}}, 201)
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
                if not self._business_authenticated():
                    return self._auth_required()
                return _json(self, download_and_install(str(payload["slug"]), str(payload["version"])), 201)
            if re.fullmatch(r"/api/runs/[^/]+/emergency-stop", parsed.path):
                pass
            elif not self._business_authenticated():
                return self._auth_required()
            payload = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(payload, dict):
                raise TypeError("request body must be a JSON object")
            if self.path == "/api/marketplace/recommendations":
                from edgepilot.marketplace import recommend
                return _json(self, recommend(payload))
            if self.path == "/api/backtests":
                try:
                    return _json(self, {"job_id": _start_backtest(payload)}, 202)
                except Exception as exc:
                    from edgepilot.backtest import CatalogDataRequiredError
                    if isinstance(exc, CatalogDataRequiredError):
                        return _error(self, "DATA_REQUIRED", "market data must be prepared before this backtest", 409,
                                      catalog_path=str(exc.catalog_path), requirements=exc.requirements)
                    raise
            strategy_config_match = re.fullmatch(r"/api/strategies/([^/]+)/configs/([^/]+)", self.path)
            if strategy_config_match:
                if set(payload) != {"config"}:
                    raise ValueError("configuration request body must contain only config")
                values = _create_strategy_config(
                    strategy_config_match.group(1),
                    strategy_config_match.group(2),
                    payload.get("config"),
                )
                return _json(self, {"saved": strategy_config_match.group(2), "config": values}, 201)
            if self.path == "/api/data/pull":
                return _json(self, {"job_id": _start_data_pull(payload)}, 202)
            if self.path == "/api/credentials":
                return _json(self, _save_credentials(payload), 201)
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
            if not self._business_authenticated():
                return self._auth_required()
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
            values = _update_strategy_config(
                strategy_config_match.group(1),
                strategy_config_match.group(2),
                payload.get("config"),
            )
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
            if not self._business_authenticated():
                return self._auth_required()
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
    LOGGER.info("dashboard started", extra={"event": "dashboard.started", "params": {"host": host, "port": port}})
    print(f"EdgePilot dashboard: http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        LOGGER.info("dashboard stopped", extra={"event": "dashboard.stopped", "result": "success"})
