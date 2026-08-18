from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from edgepilot_backtest_core.discovery import StrategyDescriptor, instantiate_config_class


def preset_names(root: Path, strategy: StrategyDescriptor) -> list[str]:
    path = root / strategy.name / "configs"
    return sorted(item.stem for item in path.glob("*.json")) if path.exists() else []


def load_preset(root: Path, strategy: StrategyDescriptor, name: str | None) -> tuple[str | None, dict[str, Any]]:
    available = preset_names(root, strategy)
    selected = "default" if name is None and "default" in available else name
    if selected is None:
        return None, {}
    path = root / strategy.name / "configs" / f"{selected}.json"
    if not path.exists():
        raise FileNotFoundError(f"Unknown preset {selected!r} for {strategy.name}; available: {available}")
    values = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(values, dict):
        raise TypeError(f"Strategy preset must contain a JSON object: {path}")
    return selected, values


def resolve_strategy_parameters(strategy: StrategyDescriptor, values: dict[str, Any]) -> dict[str, Any]:
    return json.loads(instantiate_config_class(strategy.config_cls, values).json())


def preset_strategy_values(preset: dict[str, Any]) -> dict[str, Any]:
    values = preset.get("strategy", preset)
    if not isinstance(values, dict):
        raise TypeError("Preset 'strategy' must be a JSON object")
    return dict(values)


def preset_backtest_values(preset: dict[str, Any]) -> dict[str, Any]:
    values = preset.get("backtest", {})
    if not isinstance(values, dict):
        raise TypeError("Preset 'backtest' must be a JSON object")
    return dict(values)


def preset_markets(preset: dict[str, Any]) -> list[dict[str, Any]]:
    values = preset_backtest_values(preset).get("markets")
    if not isinstance(values, list) or not values:
        raise ValueError("Preset backtest.markets must be a non-empty array")
    result = []
    for market in values:
        if not isinstance(market, dict):
            raise TypeError("Each backtest market must be an object")
        missing = {"instrument_id", "bar_type", "venue"} - market.keys()
        if missing:
            raise ValueError(f"Market is missing required fields: {sorted(missing)}")
        result.append(dict(market))
    return result


def preset_venues(preset: dict[str, Any]) -> dict[str, dict[str, Any]]:
    values = preset_backtest_values(preset).get("venues")
    if not isinstance(values, dict) or not values:
        raise ValueError("Preset backtest.venues must be a non-empty object")
    result = {}
    for name, settings in values.items():
        if not isinstance(settings, dict):
            raise TypeError(f"Venue settings for {name} must be an object")
        result[str(name).upper()] = dict(settings)
    return result


def public_adapter_options(values: dict[str, Any]) -> dict[str, Any]:
    secrets = ("api_key", "api_secret", "passphrase", "password", "private_key", "secret", "token")
    return {key: value for key, value in values.items() if not any(part in key.lower() for part in secrets)}
