"""Pure Strategy Workspace v1 projection and five-field validation."""

from __future__ import annotations

from hashlib import sha256
import json
import math
from pathlib import Path
import re
from typing import Any

from edgepilot.platform.paths import strategies_state_root
from edgepilot.strategies.configuration_store import create_configuration
from edgepilot.strategies.configuration_store import delete_configuration
from edgepilot.strategies.configuration_store import list_configuration_names
from edgepilot.strategies.configuration_store import load_configuration
from edgepilot.strategies.configuration_store import update_configuration
from edgepilot.strategies.configuration_store import validate_configuration_name
from edgepilot.strategies.configuration_store import validate_strategy_name


WORKSPACE_SCHEMA_VERSION = 1
SUPPORTED_PERIOD_DAYS = (30, 90, 365)
_MAX_JSON_BYTES = 512 * 1024
_DEFAULT_MAKER_FEE_BPS = 2.0
_DEFAULT_TAKER_FEE_BPS = 5.0
_STANDARD_LEVERAGE_OPTIONS = (1.0, 2.0, 3.0, 5.0, 10.0, 20.0, 25.0, 50.0, 100.0, 125.0)


class UnsupportedVenueModelError(ValueError):
    """Raised when an execution path receives a multi-venue target in v1."""


def _strategy_directory(strategy: str) -> Path:
    root = strategies_state_root().resolve()
    path = (root / validate_strategy_name(strategy)).resolve()
    if path.parent != root or not path.is_dir():
        raise FileNotFoundError(strategy)
    return path


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink() or path.stat().st_size > _MAX_JSON_BYTES:
        raise FileNotFoundError(path.name)
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return value


def _preset_path(directory: Path, name: str) -> Path:
    path = (directory / "configs" / f"{validate_configuration_name(name)}.json").resolve()
    if path.parent != (directory / "configs").resolve():
        raise ValueError("invalid preset path")
    return path


def _manifest(directory: Path) -> dict[str, Any]:
    try:
        return _read_json(directory / "marketplace.json")
    except (FileNotFoundError, ValueError, OSError, json.JSONDecodeError):
        return {}


def _configuration_names(directory: Path, strategy: str) -> list[dict[str, str]]:
    configs = directory / "configs"
    package = []
    if configs.is_dir():
        for path in configs.glob("*.json"):
            if path.is_file() and re.fullmatch(r"[a-z][a-z0-9_-]{0,79}", path.stem):
                package.append({"name": path.stem, "source": "package" if path.stem == "default" else "legacy"})
    names = {item["name"]: item for item in package}
    for name in list_configuration_names(strategy):
        names[name] = {"name": name, "source": "user_override" if name in names else "user_created"}
    return sorted(names.values(), key=lambda item: (item["name"] != "default", item["name"]))


def _leverage_options(manifest: dict[str, Any], default: float) -> list[float]:
    profile = manifest.get("execution_profile") if isinstance(manifest.get("execution_profile"), dict) else {}
    maximum = profile.get("maximum_supported_leverage", 10)
    if isinstance(maximum, bool) or not isinstance(maximum, (int, float)) or not math.isfinite(maximum):
        maximum = 10
    options = [value for value in _STANDARD_LEVERAGE_OPTIONS if value <= float(maximum)]
    if default > 0 and math.isfinite(default) and default not in options:
        options.append(default)
    return sorted(options)


def _fee_or_default(values: dict[str, Any], field: str, default: float) -> Any:
    value = values.get(field)
    return default if value is None else value


def _target_from_preset(target_id: str, preset: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
    from edgepilot_core.backtest.preset_schema import preset_markets
    from edgepilot_core.backtest.preset_schema import preset_venues
    from edgepilot_core.backtest.preset_schema import resolve_preset

    resolved = resolve_preset(preset, target_id)
    venue_values = preset_venues(resolved)
    markets = preset_markets(resolved)
    legs = []
    for venue, values in venue_values.items():
        venue_markets = [
            {
                "instrument_id": str(market["instrument_id"]),
                "bar_type": str(market["bar_type"]),
                "venue": str(market["venue"]).upper(),
                "data_type": str(market.get("data_type", "bars")),
            }
            for market in markets
            if str(market.get("venue", "")).upper() == venue
        ]
        if not venue_markets:
            raise ValueError(f"target {target_id} has no markets for venue {venue}")
        default_leverage = float(values.get("default_leverage", 1.0))
        legs.append({
            "id": venue,
            "venue": venue,
            "markets": venue_markets,
            "base_currency": str(values.get("base_currency", "USDT")),
            "account_type": str(values.get("account_type", "MARGIN")),
            "oms_type": str(values.get("oms_type", "NETTING")),
            "defaults": {
                "starting_balance": float(values.get("starting_balance", 100_000.0)),
                "leverage": default_leverage,
                "maker_fee_bps": _fee_or_default(values, "maker_fee_bps", _DEFAULT_MAKER_FEE_BPS),
                "taker_fee_bps": _fee_or_default(values, "taker_fee_bps", _DEFAULT_TAKER_FEE_BPS),
            },
            "constraints": {
                "starting_balance": {"minimum": 1.0, "maximum": 1_000_000_000_000.0},
                "leverage_options": _leverage_options(manifest, default_leverage),
                "fee_bps": {"minimum": 0.0, "maximum": 10_000.0},
            },
        })
    return {
        "id": target_id,
        "label": " + ".join(leg["venue"] for leg in legs),
        "venue_model": "single_venue" if len(legs) == 1 else "multi_venue",
        "legs": legs,
    }


def _targets(preset: dict[str, Any], manifest: dict[str, Any]) -> list[dict[str, Any]]:
    from edgepilot_core.backtest.preset_schema import preset_markets
    from edgepilot_core.backtest.preset_schema import preset_venue_options
    from edgepilot_core.backtest.preset_schema import preset_venues

    options = preset_venue_options(preset)
    if not options:
        configured = list(preset_venues(preset))
        options = configured if len(configured) == 1 else ["+".join(configured)]
    result = []
    for target_id in options:
        if "+" in target_id:
            resolved = dict(preset)
            venue_values = preset_venues(resolved)
            markets = preset_markets(resolved)
            legs = []
            for venue, values in venue_values.items():
                venue_markets = [dict(market) for market in markets if str(market.get("venue", "")).upper() == venue]
                if not venue_markets:
                    raise ValueError(f"target {target_id} has no markets for venue {venue}")
                default_leverage = float(values.get("default_leverage", 1.0))
                legs.append({
                    "id": venue,
                    "venue": venue,
                    "markets": venue_markets,
                    "base_currency": str(values.get("base_currency", "USDT")),
                    "account_type": str(values.get("account_type", "MARGIN")),
                    "oms_type": str(values.get("oms_type", "NETTING")),
                    "defaults": {
                        "starting_balance": float(values.get("starting_balance", 100_000.0)),
                        "leverage": default_leverage,
                        "maker_fee_bps": _fee_or_default(values, "maker_fee_bps", _DEFAULT_MAKER_FEE_BPS),
                        "taker_fee_bps": _fee_or_default(values, "taker_fee_bps", _DEFAULT_TAKER_FEE_BPS),
                    },
                    "constraints": {
                        "starting_balance": {"minimum": 1.0, "maximum": 1_000_000_000_000.0},
                        "leverage_options": _leverage_options(manifest, default_leverage),
                        "fee_bps": {"minimum": 0.0, "maximum": 10_000.0},
                    },
                })
            result.append({"id": target_id, "label": " + ".join(venue_values), "venue_model": "multi_venue", "legs": legs})
        else:
            result.append(_target_from_preset(target_id, preset, manifest))
    return result


def _default_settings(target: dict[str, Any]) -> dict[str, dict[str, float | None]]:
    return {
        "starting_balances": {leg["id"]: leg["defaults"].get("starting_balance") for leg in target["legs"]},
        "leverages": {leg["id"]: leg["defaults"].get("leverage") for leg in target["legs"]},
        "maker_fee_bps": {leg["id"]: leg["defaults"].get("maker_fee_bps") for leg in target["legs"]},
        "taker_fee_bps": {leg["id"]: leg["defaults"].get("taker_fee_bps") for leg in target["legs"]},
    }


def _settings_with_fee_defaults(
    settings: dict[str, dict[str, float | None]],
    target: dict[str, Any],
) -> dict[str, dict[str, float | None]]:
    normalized = {field: dict(values) for field, values in settings.items()}
    defaults = _default_settings(target)
    for field in ("maker_fee_bps", "taker_fee_bps"):
        for leg in target["legs"]:
            leg_id = leg["id"]
            if normalized[field][leg_id] is None:
                normalized[field][leg_id] = defaults[field][leg_id]
    return normalized


def _number(value: Any, label: str, minimum: float, maximum: float, *, allow_none: bool = False) -> float | None:
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{label} must be a finite number")
    number = float(value)
    if not minimum <= number <= maximum:
        raise ValueError(f"{label} must be between {minimum:g} and {maximum:g}")
    return number


def validate_settings(settings: Any, target: dict[str, Any]) -> dict[str, dict[str, float | None]]:
    if not isinstance(settings, dict) or set(settings) != {
        "starting_balances", "leverages", "maker_fee_bps", "taker_fee_bps",
    }:
        raise ValueError("configuration settings fields are invalid")
    leg_ids = {leg["id"] for leg in target["legs"]}
    result: dict[str, dict[str, float | None]] = {}
    for field in ("starting_balances", "leverages", "maker_fee_bps", "taker_fee_bps"):
        values = settings.get(field)
        if not isinstance(values, dict) or set(values) != leg_ids:
            raise ValueError(f"{field} must cover exactly the target legs")
        result[field] = {}
        for leg in target["legs"]:
            leg_id = leg["id"]
            if field == "starting_balances":
                result[field][leg_id] = _number(values[leg_id], field, 1.0, 1_000_000_000_000.0)
            elif field == "leverages":
                leverage = _number(values[leg_id], field, 1.0, 125.0)
                if leverage not in leg["constraints"]["leverage_options"]:
                    raise ValueError(f"leverage is not supported for {leg_id}")
                result[field][leg_id] = leverage
            else:
                result[field][leg_id] = _number(values[leg_id], field, 0.0, 10_000.0, allow_none=True)
    return result


def strategy_workspace(strategy: str, configuration: str = "default", locale: str = "en") -> dict[str, Any]:
    directory = _strategy_directory(strategy)
    manifest = _manifest(directory)
    stored = load_configuration(strategy, configuration)
    base_preset = str(stored.get("base_preset")) if stored else validate_configuration_name(configuration)
    preset_path = _preset_path(directory, base_preset)
    preset_bytes = preset_path.read_bytes()
    if len(preset_bytes) > _MAX_JSON_BYTES:
        raise ValueError("preset is too large")
    preset = json.loads(preset_bytes)
    if not isinstance(preset, dict):
        raise ValueError("preset must contain a JSON object")
    digest = sha256(preset_bytes).hexdigest()
    targets = _targets(preset, manifest)
    if not targets:
        raise ValueError("preset has no execution target")
    selected_id = str(stored.get("target_id")) if stored else targets[0]["id"]
    selected = next((target for target in targets if target["id"] == selected_id), None)
    if selected is None:
        raise ValueError(f"configuration target is unavailable: {selected_id}")
    settings = (
        _settings_with_fee_defaults(validate_settings(stored["settings"], selected), selected)
        if stored
        else _default_settings(selected)
    )
    translations = manifest.get("translations") if isinstance(manifest.get("translations"), dict) else {}
    translated = translations.get(locale) if locale != "en" and isinstance(translations.get(locale), dict) else {}
    display_name = translated.get("name") if isinstance(translated.get("name"), str) else manifest.get("name")
    source = "package" if not stored and configuration == "default" else "legacy" if not stored else "user_override" if _preset_path(directory, configuration).exists() else "user_created"
    return {
        "schema_version": WORKSPACE_SCHEMA_VERSION,
        "strategy": {
            "name": strategy,
            "display_name": display_name if isinstance(display_name, str) and display_name else strategy,
            "venue_model": selected["venue_model"],
            "venue_model_supported": selected["venue_model"] == "single_venue" and len(selected["legs"]) == 1,
        },
        "configurations": _configuration_names(directory, strategy),
        "active_configuration": {
            "name": configuration,
            "source": source,
            "revision": int(stored["revision"]) if stored else 0,
            "stale": bool(stored and stored["base_config_sha256"] != digest),
            "base_preset": base_preset,
            "base_config_sha256": digest,
            "selected_target_id": selected["id"],
            "targets": targets,
            "settings": settings,
            "constraints": {leg["id"]: leg["constraints"] for leg in selected["legs"]},
            "locked": {"legs": selected["legs"]},
        },
        "capabilities": {
            "period_days": list(SUPPORTED_PERIOD_DAYS),
            "multi_venue_execution": False,
        },
    }


def ensure_single_venue_workspace(workspace: dict[str, Any]) -> None:
    strategy = workspace.get("strategy", {})
    active = workspace.get("active_configuration", {})
    targets = active.get("targets", [])
    selected = next((target for target in targets if target.get("id") == active.get("selected_target_id")), None)
    if strategy.get("venue_model") != "single_venue" or not isinstance(selected, dict) or len(selected.get("legs", [])) != 1:
        raise UnsupportedVenueModelError("MULTI_VENUE_NOT_SUPPORTED: Live Strategy Workspace v1 supports one venue")


def resolved_workspace_plan(workspace: dict[str, Any], *, period_days: int | None = None) -> dict[str, Any]:
    ensure_single_venue_workspace(workspace)
    active = workspace["active_configuration"]
    if active["stale"]:
        raise ValueError("STALE_CONFIGURATION: review the updated package settings before running")
    if period_days is not None and period_days not in SUPPORTED_PERIOD_DAYS:
        raise ValueError("period_days must be 30, 90, or 365")
    target = next(item for item in active["targets"] if item["id"] == active["selected_target_id"])
    leg = target["legs"][0]
    settings = validate_settings(active["settings"], target)
    plan = {
        "schema_version": WORKSPACE_SCHEMA_VERSION,
        "strategy": workspace["strategy"]["name"],
        "configuration": active["name"],
        "configuration_revision": active["revision"],
        "base_preset": active["base_preset"],
        "base_config_sha256": active["base_config_sha256"],
        "venue_model": "single_venue",
        "target_id": target["id"],
        "venue": leg["venue"],
        "settings": settings,
        "period_days": period_days,
    }
    plan["configuration_sha256"] = sha256(
        json.dumps(plan, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8"),
    ).hexdigest()
    return plan


def _validated_configuration_values(
    workspace: dict[str, Any],
    *,
    target_id: str,
    settings: Any,
) -> dict[str, Any]:
    ensure_single_venue_workspace(workspace)
    active = workspace["active_configuration"]
    target = next((item for item in active["targets"] if item["id"] == target_id), None)
    if target is None:
        raise ValueError(f"configuration target is unavailable: {target_id}")
    if target["venue_model"] != "single_venue" or len(target["legs"]) != 1:
        raise UnsupportedVenueModelError("MULTI_VENUE_NOT_SUPPORTED: Live Strategy Workspace v1 supports one venue")
    validated_settings = _settings_with_fee_defaults(validate_settings(settings, target), target)
    return {
        "base_preset": active["base_preset"],
        "base_config_sha256": active["base_config_sha256"],
        "target_id": target_id,
        "settings": validated_settings,
    }


def create_workspace_configuration(
    strategy: str,
    name: str,
    *,
    base_configuration: str,
    target_id: str,
    settings: Any,
    locale: str = "en",
) -> dict[str, Any]:
    base = strategy_workspace(strategy, base_configuration, locale)
    values = _validated_configuration_values(base, target_id=target_id, settings=settings)
    create_configuration(strategy, name, values)
    return strategy_workspace(strategy, name, locale)


def update_workspace_configuration(
    strategy: str,
    name: str,
    *,
    expected_revision: int,
    target_id: str,
    settings: Any,
    locale: str = "en",
) -> dict[str, Any]:
    current = strategy_workspace(strategy, name, locale)
    if current["active_configuration"]["revision"] < 1:
        raise FileNotFoundError(name)
    values = _validated_configuration_values(current, target_id=target_id, settings=settings)
    update_configuration(strategy, name, values, expected_revision=expected_revision)
    return strategy_workspace(strategy, name, locale)


def reset_workspace_configuration(
    strategy: str,
    name: str,
    *,
    expected_revision: int | None = None,
    locale: str = "en",
) -> dict[str, Any]:
    current = load_configuration(strategy, name)
    if current is None:
        raise FileNotFoundError(name)
    base_preset = str(current["base_preset"])
    delete_configuration(strategy, name, expected_revision=expected_revision)
    fallback = name if _preset_path(_strategy_directory(strategy), name).is_file() else base_preset
    return strategy_workspace(strategy, fallback, locale)
