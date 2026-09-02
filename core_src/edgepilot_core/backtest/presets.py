from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from edgepilot_core.backtest.discovery import StrategyDescriptor, instantiate_config_class
# Structure and venue rules live in a module free of engine imports so the Dashboard
# and Research HTTP layers can apply the same ones without loading NautilusTrader.
from edgepilot_core.backtest.preset_schema import VARIANT_SECTIONS  # noqa: F401
from edgepilot_core.backtest.preset_schema import VARIANT_STRATEGY_SUFFIXES  # noqa: F401
from edgepilot_core.backtest.preset_schema import VENUE_VARIANTS_KEY  # noqa: F401
from edgepilot_core.backtest.preset_schema import benchmark_venue  # noqa: F401
from edgepilot_core.backtest.preset_schema import preset_backtest_values  # noqa: F401
from edgepilot_core.backtest.preset_schema import preset_markets  # noqa: F401
from edgepilot_core.backtest.preset_schema import preset_strategy_values  # noqa: F401
from edgepilot_core.backtest.preset_schema import preset_venue_options  # noqa: F401
from edgepilot_core.backtest.preset_schema import preset_venues  # noqa: F401
from edgepilot_core.backtest.preset_schema import public_adapter_options  # noqa: F401
from edgepilot_core.backtest.preset_schema import resolve_preset  # noqa: F401
from edgepilot_core.backtest.preset_schema import validate_venue_variants  # noqa: F401


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
