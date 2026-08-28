from __future__ import annotations

import json
import math
from pathlib import Path
import re
from typing import Any

import matplotlib

matplotlib.use("Agg")

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from matplotlib.ticker import StrMethodFormatter
import numpy as np
import pandas as pd
from nautilus_trader.backtest.engine import BacktestEngine
from edgepilot_core.backtest.metrics import collect_metrics
from nautilus_trader.model.data import BarType
from nautilus_trader.persistence.catalog import ParquetDataCatalog


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


def export_reports(
    engine: BacktestEngine,
    run_dir: Path,
    metrics: dict[str, Any],
    *,
    catalog_path: Path,
    bar_types: list[str],
    start: Any,
    end: Any,
    starting_balance: float,
) -> None:
    """Export queryable native results and one focused visual report."""
    run_dir.mkdir(parents=True, exist_ok=True)
    fills = engine.trader.generate_order_fills_report().reset_index()
    positions = engine.trader.generate_positions_report().reset_index()
    fills.to_csv(run_dir / "fills.csv", index=False)
    positions.to_csv(run_dir / "positions.csv", index=False)
    (run_dir / "metrics.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")

    markets = []
    for bar_type in bar_types:
        bars, multiplier = _load_market(catalog_path, bar_type, start, end)
        instrument_id = str(BarType.from_str(bar_type).instrument_id)
        market_positions = positions[
            positions.get("instrument_id", pd.Series(dtype=str)).astype(str).eq(instrument_id)
        ]
        market_fills = fills[
            fills.get("instrument_id", pd.Series(dtype=str)).astype(str).eq(instrument_id)
        ]
        market_series = _build_timeseries(
            bars,
            market_positions,
            market_fills,
            starting_balance=0.0,
            multiplier=multiplier,
        )
        markets.append((instrument_id, bars, market_positions, market_series))

    series = _aggregate_timeseries(markets, starting_balance)
    series.to_json(
        run_dir / "timeseries.json",
        orient="records",
        date_format="iso",
        indent=2,
    )
    (run_dir / "market_timeseries.json").write_text(
        json.dumps(
            {
                "markets": [
                    {
                        "instrument_id": instrument_id,
                        "series": json.loads(
                            market_series[["timestamp", "close"]].to_json(
                                orient="records",
                                date_format="iso",
                            ),
                        ),
                    }
                    for instrument_id, _, _, market_series in markets
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    _export_chart(
        markets=markets,
        series=series,
        metrics=metrics,
        output_png=run_dir / "backtest.png",
        starting_balance=starting_balance,
    )


def _load_market(
    catalog_path: Path,
    bar_type: str,
    start: Any,
    end: Any,
) -> tuple[pd.DataFrame, float]:
    start_ns = int(start.timestamp() * 1_000_000_000)
    end_ns = int(end.timestamp() * 1_000_000_000)
    catalog = ParquetDataCatalog(str(catalog_path))
    rows = []
    for bar in catalog.bars(bar_types=[bar_type]):
        if start_ns <= bar.ts_event <= end_ns:
            rows.append(
                {
                    "timestamp": pd.Timestamp(bar.ts_event, unit="ns", tz="UTC"),
                    "close": bar.close.as_double(),
                },
            )
    frame = pd.DataFrame(rows).sort_values("timestamp").reset_index(drop=True)
    instrument_id = str(BarType.from_str(bar_type).instrument_id)
    instruments = catalog.instruments(instrument_ids=[instrument_id])
    if not instruments:
        raise RuntimeError(f"Instrument unavailable for report: {instrument_id}")
    return frame, float(instruments[-1].multiplier)


def _build_timeseries(
    bars: pd.DataFrame,
    positions: pd.DataFrame,
    fills: pd.DataFrame,
    starting_balance: float,
    multiplier: float,
) -> pd.DataFrame:
    frame = bars.copy()
    timestamps = frame["timestamp"].astype("int64").to_numpy()
    prices = frame["close"].to_numpy(dtype=float)
    pnl = np.zeros(len(frame), dtype=float)
    opening_fees = {
        str(row["client_order_id"]): _money_number(row.get("commissions"))
        for _, row in fills.iterrows()
    }
    for _, position in positions.iterrows():
        opened_ns = pd.Timestamp(position["ts_opened"]).value
        closed_ns = pd.Timestamp(position["ts_closed"]).value
        open_mask = (timestamps >= opened_ns) & (timestamps < closed_ns)
        closed_mask = timestamps >= closed_ns
        direction = 1.0 if str(position["entry"]) == "BUY" else -1.0
        quantity = float(position["peak_qty"])
        entry_price = float(position["avg_px_open"])
        entry_fee = opening_fees.get(str(position["opening_order_id"]), 0.0)
        pnl[open_mask] += (
            direction * (prices[open_mask] - entry_price) * quantity * multiplier - entry_fee
        )
        pnl[closed_mask] += _money_number(position["realized_pnl"])
    frame["pnl"] = pnl
    frame["equity"] = starting_balance + pnl
    peak = frame["equity"].cummax()
    frame["drawdown_pct"] = 100.0 * (frame["equity"] / peak - 1.0)
    return frame


def _aggregate_timeseries(
    markets: list[tuple[str, pd.DataFrame, pd.DataFrame, pd.DataFrame]],
    starting_balance: float,
) -> pd.DataFrame:
    timestamps = sorted({timestamp for _, _, _, series in markets for timestamp in series["timestamp"]})
    frame = pd.DataFrame({"timestamp": pd.to_datetime(timestamps, utc=True)})
    pnl = np.zeros(len(frame), dtype=float)
    target_index = pd.DatetimeIndex(frame["timestamp"])
    for _, _, _, market_series in markets:
        values = market_series.set_index("timestamp")["pnl"].reindex(target_index, method="ffill").fillna(0.0)
        pnl += values.to_numpy(dtype=float)
    frame["pnl"] = pnl
    frame["equity"] = starting_balance + pnl
    peak = frame["equity"].cummax()
    frame["drawdown_pct"] = 100.0 * (frame["equity"] / peak - 1.0)
    return frame


def _money_number(value: Any) -> float:
    match = re.search(r"[-+]?\d+(?:\.\d+)?", str(value))
    return float(match.group(0)) if match else 0.0


def _export_chart(
    *,
    markets: list[tuple[str, pd.DataFrame, pd.DataFrame, pd.DataFrame]],
    series: pd.DataFrame,
    metrics: dict[str, Any],
    output_png: Path,
    starting_balance: float,
) -> None:
    fig, axes = plt.subplots(
        len(markets) + 2,
        1,
        figsize=(18, max(13, 4.0 * len(markets) + 5.0)),
        sharex=True,
        gridspec_kw={"height_ratios": [2.0] * len(markets) + [1.2, 1.0], "hspace": 0.22},
    )
    price_axes = axes[: len(markets)]
    for price_ax, (instrument_id, bars, positions, _) in zip(price_axes, markets):
        price_ax.plot(bars["timestamp"], bars["close"], color="#243B64", linewidth=1.4, label="Price")
        opened = pd.to_datetime(positions["ts_opened"], utc=True) if "ts_opened" in positions else pd.Series(index=positions.index, dtype="datetime64[ns, UTC]")
        closed = pd.to_datetime(positions["ts_closed"], utc=True) if "ts_closed" in positions else pd.Series(index=positions.index, dtype="datetime64[ns, UTC]")
        entries = positions["entry"].astype(str) if "entry" in positions else pd.Series(index=positions.index, dtype=str)
        open_prices = positions["avg_px_open"] if "avg_px_open" in positions else pd.Series(index=positions.index, dtype=float)
        close_prices = positions["avg_px_close"] if "avg_px_close" in positions else pd.Series(index=positions.index, dtype=float)
        marker_sets = [
            (entries.eq("BUY"), opened, open_prices, "BUY entry", "^", "#009E73"),
            (entries.eq("SELL"), opened, open_prices, "SELL entry", "v", "#D55E00"),
            (pd.Series(True, index=positions.index), closed, close_prices, "Exit", "x", "#111111"),
        ]
        for mask, timestamps, prices, name, symbol, color in marker_sets:
            price_ax.scatter(
                timestamps[mask],
                prices[mask].astype(float),
                marker=symbol,
                s=42,
                color=color,
                linewidths=1,
                label=name,
                zorder=3,
            )
        price_ax.text(
            0.0,
            0.98,
            f"{instrument_id} | price and orders",
            transform=price_ax.transAxes,
            va="top",
            ha="left",
            fontsize=10,
            bbox={"facecolor": "white", "edgecolor": "none", "alpha": 0.78, "pad": 2.5},
        )
        price_ax.set_ylabel("Price (USDT)")
        if price_ax is price_axes[0]:
            price_ax.legend(loc="upper right", ncols=4, frameon=False)

    pnl_ax = axes[-2]
    pnl_ax.plot(
        series["timestamp"],
        series["pnl"],
        color="#0072B2",
        linewidth=1.8,
        label="Mark-to-market PnL",
    )
    pnl_ax.fill_between(
        series["timestamp"],
        series["pnl"],
        0.0,
        color="#0072B2",
        alpha=0.10,
    )
    pnl_ax.axhline(0.0, color="#777777", linewidth=1, linestyle="--")

    pnl_ax.set_title("Portfolio mark-to-market PnL", loc="left")
    pnl_ax.set_ylabel("PnL (USDT)")
    pnl_ax.yaxis.set_major_formatter(StrMethodFormatter("${x:,.0f}"))
    equity_ax = axes[-1]
    equity_ax.plot(series["timestamp"], series["equity"], color="#009E73", linewidth=1.8, label="Equity")
    equity_ax.set_title("Portfolio equity", loc="left")
    equity_ax.set_ylabel("USDT")
    equity_ax.set_xlabel("Date")
    equity_ax.yaxis.set_major_formatter(StrMethodFormatter("${x:,.0f}"))
    date_locator = mdates.AutoDateLocator(minticks=6, maxticks=12)
    pnl_ax.xaxis.set_major_locator(date_locator)
    pnl_ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(date_locator))
    for axis in axes:
        axis.grid(True, color="#D9DEE7", linewidth=0.7, alpha=0.65)
        axis.spines[["top", "right"]].set_visible(False)

    fig.suptitle(
        (
            f"Backtest | Annualized return {metrics['annualized_return_pct']:.2f}% | "
            f"PnL \\${metrics['realized_pnl']:,.0f} | "
            f"Max drawdown {metrics['max_drawdown_pct']:.2f}% | "
            f"Sharpe {metrics['sharpe']:.2f} | "
            f"Starting balance \\${starting_balance:,.0f}"
        ),
        fontsize=15,
        y=0.98,
    )
    fig.subplots_adjust(top=0.92, left=0.08, right=0.98, bottom=0.08)
    fig.savefig(output_png, dpi=100, facecolor="white")
    plt.close(fig)
