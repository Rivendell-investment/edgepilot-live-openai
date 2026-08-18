from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from edgepilot.discovery import StrategyDescriptor
from edgepilot.discovery import instantiate_config_class
from edgepilot.discovery import strategies_root


def _configs_path(strategy: StrategyDescriptor) -> Path:
    return strategies_root() / strategy.name / "configs"


def preset_names(strategy: StrategyDescriptor) -> list[str]:
    path = _configs_path(strategy)
    if not path.exists():
        return []
    return sorted(item.stem for item in path.glob("*.json"))


def load_preset(
    strategy: StrategyDescriptor,
    name: str | None,
) -> tuple[str | None, dict[str, Any]]:
    selected = name
    available = preset_names(strategy)
    if selected is None and "default" in available:
        selected = "default"
    if selected is None:
        return None, {}
    path = _configs_path(strategy) / f"{selected}.json"
    if not path.exists():
        raise FileNotFoundError(
            f"Unknown preset {selected!r} for {strategy.name}; available: {available}",
        )
    values = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(values, dict):
        raise TypeError(f"Strategy preset must contain a JSON object: {path}")
    return selected, values


def resolve_strategy_parameters(
    strategy: StrategyDescriptor,
    values: dict[str, Any],
) -> dict[str, Any]:
    """Validate through the native config class and return a complete JSON snapshot."""
    config = instantiate_config_class(strategy.config_cls, values)
    return json.loads(config.json())


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
    markets = []
    for market in values:
        if not isinstance(market, dict):
            raise TypeError("Each backtest market must be an object")
        required = {"instrument_id", "bar_type", "venue"}
        missing = required - market.keys()
        if missing:
            raise ValueError(f"Market is missing required fields: {sorted(missing)}")
        markets.append(dict(market))
    return markets


def preset_venues(preset: dict[str, Any]) -> dict[str, dict[str, Any]]:
    values = preset_backtest_values(preset).get("venues")
    if not isinstance(values, dict) or not values:
        raise ValueError("Preset backtest.venues must be a non-empty object")
    venues = {}
    for name, settings in values.items():
        if not isinstance(settings, dict):
            raise TypeError(f"Venue settings for {name} must be an object")
        venues[str(name).upper()] = dict(settings)
    return venues


def public_adapter_options(values: dict[str, Any]) -> dict[str, Any]:
    """Remove credentials before persisting native adapter settings."""
    secret_fragments = (
        "api_key",
        "api_secret",
        "passphrase",
        "password",
        "private_key",
        "secret",
        "token",
    )
    return {
        key: value
        for key, value in values.items()
        if not any(fragment in key.lower() for fragment in secret_fragments)
    }
