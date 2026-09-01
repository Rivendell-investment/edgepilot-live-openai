"""Preset structure and venue resolution, free of any engine dependency.

Both plugins' HTTP layers read presets outside the locked runtime, so these rules
cannot live next to anything that imports NautilusTrader.
"""

from __future__ import annotations

from typing import Any


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


VENUE_VARIANTS_KEY = "venue_variants"
# Keys a variant may override in the ``strategy`` section.  A survey of the 330
# venue presets published in the previous rollout found differences on exactly
# these suffixes and nothing else: instrument_id (329), bar_type (329), plus the
# prefixed and plural spellings (btc_/leader_/comparative_/filter_, *_ids, *_types).
# Trading-logic parameters must stay identical across venues, otherwise "the same
# strategy" becomes two different strategies and the published benchmark numbers
# stop describing what the user can actually run.
VARIANT_STRATEGY_SUFFIXES = ("instrument_id", "instrument_ids", "bar_type", "bar_types")
# ``days`` is here because how far back an exchange's history goes is a property of the
# exchange: a venue listed last quarter cannot serve the 365-day window its benchmark
# venue uses.  Everything else about a backtest is shared and stays in the preset.
VARIANT_SECTIONS = ("venues", "markets", "strategy", "days")


def _variant_table(preset: dict[str, Any]) -> dict[str, dict[str, Any]]:
    values = preset_backtest_values(preset).get(VENUE_VARIANTS_KEY)
    if values is None:
        return {}
    if not isinstance(values, dict):
        raise TypeError(f"Preset backtest.{VENUE_VARIANTS_KEY} must be a JSON object")
    return {str(name).strip().upper(): value for name, value in values.items()}


def benchmark_venue(preset: dict[str, Any]) -> str | None:
    """The venue the preset's own markets run on -- the benchmark's venue.

    ``None`` when the markets span several venues (the preset describes one
    cross-venue portfolio rather than a choice between venues), and also when they
    cannot be read at all: naming a venue is never a precondition for serving a
    preset, so a malformed one loses its venue label rather than raising here.
    """
    try:
        markets = preset_markets(preset)
    except (ValueError, TypeError):
        return None
    venues = {str(market.get("venue", "")).strip().upper() for market in markets}
    venues.discard("")
    return venues.pop() if len(venues) == 1 else None


def preset_venue_options(preset: dict[str, Any]) -> list[str]:
    """Venues this preset can be resolved to: the benchmark venue plus every variant."""
    base = benchmark_venue(preset)
    options = [base] if base else []
    options.extend(venue for venue in _variant_table(preset) if venue != base)
    return options


def validate_venue_variants(preset: dict[str, Any]) -> None:
    """Reject variants that would fail later, at deploy time or on a user's machine."""
    base = benchmark_venue(preset)
    for venue, variant in _variant_table(preset).items():
        where = f"backtest.{VENUE_VARIANTS_KEY}.{venue}"
        if not venue or not venue.replace("_", "").isalnum():
            raise ValueError(f"{where} is not a valid venue name")
        if venue == base:
            raise ValueError(f"{where} duplicates the preset's own venue; remove the variant")
        if not isinstance(variant, dict):
            raise TypeError(f"{where} must be a JSON object")
        unknown = set(variant) - set(VARIANT_SECTIONS)
        if unknown:
            raise ValueError(
                f"{where} may only override {list(VARIANT_SECTIONS)}; "
                f"remove {sorted(unknown)} -- settings that do not change with the venue "
                "belong in the preset's own backtest section",
            )
        days = variant.get("days")
        if days is not None and (isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 5000):
            raise ValueError(f"{where}.days must be an integer between 1 and 5000")
        markets = preset_markets({"backtest": variant})
        venues = preset_venues({"backtest": variant})
        if set(venues) != {venue}:
            raise ValueError(f"{where}.venues must configure exactly {venue}, got {sorted(venues)}")
        for index, market in enumerate(markets):
            if str(market.get("venue", "")).strip().upper() != venue:
                raise ValueError(f"{where}.markets[{index}].venue must be {venue}")
            suffix = str(market["instrument_id"]).rsplit(".", 1)[-1].strip().upper()
            if suffix != venue:
                raise ValueError(
                    f"{where}.markets[{index}].instrument_id belongs to {suffix}, not {venue}",
                )
        overrides = variant.get("strategy", {})
        if not isinstance(overrides, dict):
            raise TypeError(f"{where}.strategy must be a JSON object")
        for key in overrides:
            if not key.endswith(VARIANT_STRATEGY_SUFFIXES):
                raise ValueError(
                    f"{where}.strategy may not override {key!r}: a variant only swaps the "
                    "instruments and bar types, never the trading logic",
                )
        instruments = {str(market["instrument_id"]).strip() for market in markets}
        for key, value in overrides.items():
            declared = [value] if isinstance(value, str) else value if isinstance(value, list) else []
            declared = [str(item).strip() for item in declared if str(item).strip()]
            if key.endswith(("instrument_id", "instrument_ids")):
                # Same rule the deploy path applies (_select_trading_venue): catching it
                # here means a broken variant never reaches a user's machine.
                stray = set(declared) - instruments
            else:
                # A bar type is "<instrument_id>-<spec>"; the spec may aggregate, so only
                # the instrument it belongs to has to be one the variant provides data for.
                stray = {
                    item for item in declared
                    if not any(item == name or item.startswith(f"{name}-") for name in instruments)
                }
            if stray:
                raise ValueError(
                    f"{where}.strategy.{key} references instruments outside the variant's "
                    f"markets: {sorted(stray)}",
                )


def resolve_preset(preset: dict[str, Any], venue: str | None = None) -> dict[str, Any]:
    """Reduce a multi-venue preset to an ordinary single-venue one.

    Every consumer downstream -- the backtest engine, the deploy path, the config
    editor -- keeps seeing the shape it has always seen.  Presets without variants
    pass through unchanged apart from dropping the (empty) variants key.
    """
    variants = _variant_table(preset)
    resolved = dict(preset)
    backtest = preset_backtest_values(preset)
    backtest.pop(VENUE_VARIANTS_KEY, None)
    resolved["backtest"] = backtest
    selected = venue.strip().upper() if isinstance(venue, str) and venue.strip() else None
    if selected is None or selected == benchmark_venue(preset):
        return resolved
    if selected not in variants:
        raise ValueError(
            f"Preset does not support venue {selected}; available: {preset_venue_options(preset)}",
        )
    validate_venue_variants(preset)
    variant = variants[selected]
    backtest["venues"] = dict(variant["venues"])
    backtest["markets"] = [dict(market) for market in variant["markets"]]
    if "days" in variant:
        backtest["days"] = variant["days"]
    resolved["strategy"] = {**preset_strategy_values(preset), **variant.get("strategy", {})}
    return resolved


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
