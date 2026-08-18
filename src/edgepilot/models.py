from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from edgepilot.discovery import AdapterDescriptor


@dataclass(frozen=True)
class MarketRequest:
    """One market-data leg in a run."""

    instrument_id: str
    bar_type: str
    venue: str
    data_type: str = "bars"


@dataclass(frozen=True)
class VenueRequest:
    """One independent Nautilus venue/account in a run."""

    adapter: AdapterDescriptor
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
