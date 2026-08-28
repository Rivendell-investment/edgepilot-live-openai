"""Nautilus-dependent operations executed only by the locked Live runtime."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import logging
import os
from pathlib import Path
import tempfile
from typing import Any

from edgepilot.dashboard.common import ConfigConflictError
from edgepilot.dashboard.common import safe_config_name
from edgepilot.dashboard.common import safe_directory
from edgepilot.strategies.discovery import discover_execution_adapters
from edgepilot.strategies.discovery import resolve_adapter
from edgepilot.strategies.discovery import resolve_strategy
from edgepilot.strategies.discovery import strategies_root
from edgepilot.execution.environment import credential_requirements
from edgepilot.platform.paths import account_credentials_path, state_root
from edgepilot.strategies.presets import load_preset
from edgepilot.strategies.presets import preset_backtest_values
from edgepilot.strategies.presets import preset_markets
from edgepilot.strategies.presets import preset_names
from edgepilot.strategies.presets import preset_strategy_values
from edgepilot.strategies.presets import preset_venues
from edgepilot.strategies.presets import resolve_strategy_parameters


STATE = state_root()
LOGGER = logging.getLogger("edgepilot.dashboard.native")


def strategy_config(payload: dict[str, Any]) -> dict[str, Any]:
    strategy = resolve_strategy(str(payload.get("strategy", "")))
    name, values = load_preset(strategy, safe_config_name(str(payload.get("name", ""))))
    return {"strategy": strategy.name, "name": name, "config": values}


def strategy_detail(payload: dict[str, Any]) -> dict[str, Any]:
    strategy = resolve_strategy(str(payload.get("strategy", "")))
    return {
        "name": strategy.name,
        "strategy_path": strategy.strategy_path,
        "config_path": strategy.config_path,
        "presets": preset_names(strategy),
        "config_schema": strategy.config_cls.json_schema(),
    }


def prepare_backtest(payload: dict[str, Any]) -> dict[str, str]:
    strategy = resolve_strategy(str(payload.get("strategy", "")))
    requested_preset = safe_config_name(str(payload.get("preset", "")))
    if requested_preset not in preset_names(strategy):
        raise ValueError(f"unknown preset: {requested_preset}")
    preset_name, values = load_preset(strategy, requested_preset)
    backtest = preset_backtest_values(values)
    days = int(backtest.get("days", 365))
    if not 1 <= days <= 5000:
        raise ValueError("Preset backtest.days must be between 1 and 5000")
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=min(days, 90))
    return {
        "strategy": strategy.name,
        "preset": preset_name,
        "start": start.isoformat(),
        "end": end.isoformat(),
    }


def credential_records() -> list[dict[str, Any]]:
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


def save_credentials(payload: dict[str, Any]) -> dict[str, Any]:
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
    env_path = account_credentials_path()
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


def create_strategy_config(strategy_name: str, config_name: str, values: Any) -> dict[str, Any]:
    strategy = resolve_strategy(strategy_name)
    safe_name = safe_config_name(config_name)
    if safe_name == "default":
        raise ConfigConflictError("default is a read-only recommended configuration; save it under a new name")
    existing = safe_directory(strategies_root() / strategy.name / "configs", f"{safe_name}.json")
    if existing.exists():
        raise ConfigConflictError(f"configuration already exists: {config_name}")
    path, snapshot = validated_strategy_config(strategy_name, config_name, values)
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


def update_strategy_config(strategy_name: str, config_name: str, values: Any) -> dict[str, Any]:
    strategy = resolve_strategy(strategy_name)
    safe_name = safe_config_name(config_name)
    if safe_name == "default":
        raise ConfigConflictError("default is a read-only recommended configuration; save it under a new name")
    path = safe_directory(strategies_root() / strategy.name / "configs", f"{safe_name}.json")
    if not path.exists():
        raise FileNotFoundError(config_name)
    path, snapshot = validated_strategy_config(strategy_name, config_name, values)
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


def validated_strategy_config(strategy_name: str, config_name: str, values: Any) -> tuple[Path, dict[str, Any]]:
    strategy = resolve_strategy(strategy_name)
    config_name = safe_config_name(config_name)
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
    path = safe_directory(directory, f"{config_name}.json")
    snapshot = {**values, "strategy": strategy_values}
    return path, snapshot


def _credential_venues() -> list[str]:
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


def _config_bytes(values: dict[str, Any]) -> bytes:
    return (json.dumps(values, indent=2, sort_keys=True) + "\n").encode("utf-8")
