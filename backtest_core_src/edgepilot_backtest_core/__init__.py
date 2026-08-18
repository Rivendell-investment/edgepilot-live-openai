"""Live-adapter-free building blocks shared by EdgePilot runtimes."""

__version__ = "0.1.0"

from edgepilot_backtest_core.discovery import StrategyDescriptor
from edgepilot_backtest_core.discovery import instantiate_config
from edgepilot_backtest_core.discovery import instantiate_config_class
from edgepilot_backtest_core.discovery import resolve_strategy
from edgepilot_backtest_core.metrics import collect_metrics
from edgepilot_backtest_core.models import BacktestRequest
from edgepilot_backtest_core.models import MarketRequest
from edgepilot_backtest_core.models import VenueRequest
from edgepilot_backtest_core.runner import execute_local_backtest

__all__ = [
    "BacktestRequest",
    "MarketRequest",
    "StrategyDescriptor",
    "VenueRequest",
    "__version__",
    "collect_metrics",
    "execute_local_backtest",
    "instantiate_config",
    "instantiate_config_class",
    "resolve_strategy",
]
