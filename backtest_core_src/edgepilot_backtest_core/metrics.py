from __future__ import annotations

import math
from typing import Any

from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.model.currencies import Currency


def _number(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        try:
            parsed = float(str(value).split()[0])
        except (TypeError, ValueError):
            return default
    return parsed if math.isfinite(parsed) else default


def _find(stats: dict[str, Any], *fragments: str) -> float:
    for fragment in fragments:
        for key, value in stats.items():
            if fragment.lower() in key.lower():
                return _number(value)
    return 0.0


def collect_metrics(engine: BacktestEngine, *, base_currency: str, starting_balance: float) -> dict[str, Any]:
    analyzer = engine.portfolio.analyzer
    pnl_stats = analyzer.get_performance_stats_pnls(Currency.from_str(base_currency))
    return_stats = analyzer.get_performance_stats_returns()
    general_stats = analyzer.get_performance_stats_general()
    all_stats = {**general_stats, **pnl_stats, **return_stats}
    pnl = _find(pnl_stats, "pnl (total)", "total pnl")
    result = engine.get_result()
    return {
        "return_pct": 100.0 * pnl / starting_balance,
        "realized_pnl": pnl,
        "max_drawdown_pct": 100.0 * abs(_find(all_stats, "max drawdown")),
        "sharpe": _find(all_stats, "sharpe ratio", "sharpe"),
        "sortino": _find(all_stats, "sortino ratio", "sortino"),
        "win_rate": _find(all_stats, "win rate"),
        "profit_factor": _find(all_stats, "profit factor"),
        "orders": result.total_orders,
        "positions": result.total_positions,
    }
