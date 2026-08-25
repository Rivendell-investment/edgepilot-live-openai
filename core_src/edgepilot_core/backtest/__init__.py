"""Live-adapter-free backtest building blocks shared by EdgePilot runtimes."""

_EXPORTS = {
    "StrategyDescriptor": ("edgepilot_core.backtest.discovery", "StrategyDescriptor"),
    "instantiate_config": ("edgepilot_core.backtest.discovery", "instantiate_config"),
    "instantiate_config_class": ("edgepilot_core.backtest.discovery", "instantiate_config_class"),
    "resolve_strategy": ("edgepilot_core.backtest.discovery", "resolve_strategy"),
    "collect_metrics": ("edgepilot_core.backtest.metrics", "collect_metrics"),
    "BacktestRequest": ("edgepilot_core.backtest.models", "BacktestRequest"),
    "MarketRequest": ("edgepilot_core.backtest.models", "MarketRequest"),
    "VenueRequest": ("edgepilot_core.backtest.models", "VenueRequest"),
    "execute_local_backtest": ("edgepilot_core.backtest.runner", "execute_local_backtest"),
}


def __getattr__(name: str):
    """Keep the package import light until a native backtest symbol is used."""
    try:
        module_name, symbol = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(name) from exc
    from importlib import import_module
    value = getattr(import_module(module_name), symbol)
    globals()[name] = value
    return value

__all__ = [
    "BacktestRequest",
    "MarketRequest",
    "StrategyDescriptor",
    "VenueRequest",
    "collect_metrics",
    "execute_local_backtest",
    "instantiate_config",
    "instantiate_config_class",
    "resolve_strategy",
]
