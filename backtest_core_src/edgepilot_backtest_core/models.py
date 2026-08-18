from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from edgepilot_backtest_core.discovery import StrategyDescriptor


@dataclass(frozen=True)
class MarketRequest:
    instrument_id: str
    bar_type: str
    venue: str
    data_type: str = "bars"


@dataclass(frozen=True)
class VenueRequest:
    name: str
    adapter_options: dict[str, Any] = field(default_factory=dict)
    starting_balance: float = 100_000.0
    base_currency: str = "USDT"
    account_type: str = "MARGIN"
    oms_type: str = "NETTING"
    maker_fee_bps: float | None = None
    taker_fee_bps: float | None = None
    default_leverage: float = 1.0
    leverages: dict[str, float] | None = None
    allow_cash_borrowing: bool = False
    liquidation_enabled: bool = False
    liquidation_trigger_ratio: float = 1.0
    liquidation_cancel_open_orders: bool = True


@dataclass(frozen=True)
class BacktestRequest:
    strategy: StrategyDescriptor
    markets: tuple[MarketRequest, ...]
    venues: tuple[VenueRequest, ...]
    start: datetime
    end: datetime
    parameters: dict[str, Any]
    catalog_path: Path
    runs_path: Path
    preset_name: str | None = None
