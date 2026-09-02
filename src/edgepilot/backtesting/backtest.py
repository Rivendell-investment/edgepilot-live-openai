from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from functools import partial
import logging
from pathlib import Path
from time import monotonic
from typing import Any
from typing import get_type_hints

from nautilus_trader.common.config import resolve_path

from edgepilot.backtesting.catalog import missing_bar_intervals
from edgepilot.backtesting.catalog import pull_data
from edgepilot.backtesting.models import MarketRequest
from edgepilot.backtesting.models import VenueRequest
from edgepilot.backtesting.reporting import export_reports
from edgepilot.strategies.discovery import StrategyDescriptor
from edgepilot_core.backtest.models import BacktestRequest as CoreBacktestRequest
from edgepilot_core.backtest.models import MarketRequest as CoreMarketRequest
from edgepilot_core.backtest.models import VenueRequest as CoreVenueRequest
from edgepilot_core.backtest.runner import execute_local_backtest


LOGGER = logging.getLogger("edgepilot.backtesting.backtest")


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
    download: bool = True  # Accepted for legacy callers; missing data is always downloaded.
    export_artifacts: bool = True
    export_chart: bool = True
    preset_name: str | None = None


def _download_adapter_options(venue: VenueRequest) -> dict[str, Any]:
    """Build automatic download options with only natively supported account types."""
    options = dict(venue.adapter_options)
    config_cls = resolve_path(venue.adapter.data_config_path)
    supported_fields = get_type_hints(config_cls)
    if (
        "account_type" in supported_fields
        and str(venue.account_type).upper() != "MARGIN"
    ):
        options["account_type"] = venue.account_type
    return options


def execute_backtest(request: BacktestRequest) -> tuple[str, dict[str, Any]]:
    started = monotonic()
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    LOGGER.info("backtest started", extra={"event": "backtest.started", "run_id": run_id, "params": {"strategy": request.strategy.name, "preset": request.preset_name, "start": request.start.isoformat(), "end": request.end.isoformat(), "markets": [market.__dict__ for market in request.markets], "export_artifacts": request.export_artifacts, "export_chart": request.export_chart}})
    if not request.markets:
        raise ValueError("A backtest requires at least one market")
    venue_by_name = {venue.adapter.name: venue for venue in request.venues}
    if set(venue_by_name) != {market.venue.upper() for market in request.markets}:
        raise ValueError("Backtest venues must exactly cover the configured market venues")
    for market in request.markets:
        instrument_venue = market.instrument_id.rsplit(".", 1)[-1].upper()
        if instrument_venue != market.venue.upper():
            raise ValueError(
                f"Market venue mismatch: {market.instrument_id} belongs to {instrument_venue}, "
                f"but is configured under {market.venue.upper()}"
            )
    if any(market.data_type != "bars" for market in request.markets):
        raise ValueError("The backtest engine currently requires bar markets")

    for market in request.markets:
        venue = venue_by_name[market.venue.upper()]
        for start, end in missing_bar_intervals(
            request.catalog_path,
            market.bar_type,
            request.start,
            request.end,
        ):
            try:
                pull_data(
                    catalog_path=request.catalog_path,
                    adapter=venue.adapter,
                    instrument_id=market.instrument_id,
                    data_type=market.data_type,
                    start=start,
                    end=end,
                    bar_type=market.bar_type,
                    adapter_options=_download_adapter_options(venue),
                )
            except Exception as exc:
                raise RuntimeError(
                    "Automatic market-data preparation failed for "
                    f"{market.instrument_id} on {market.venue}: {exc}",
                ) from exc

    core_request = CoreBacktestRequest(
        strategy=request.strategy,
        markets=tuple(
            CoreMarketRequest(
                instrument_id=market.instrument_id,
                bar_type=market.bar_type,
                venue=market.venue,
                data_type=market.data_type,
            )
            for market in request.markets
        ),
        venues=tuple(
            CoreVenueRequest(
                name=venue.adapter.name,
                adapter_options=venue.adapter_options,
                starting_balance=venue.starting_balance,
                base_currency=venue.base_currency,
                account_type=venue.account_type,
                oms_type=venue.oms_type,
                maker_fee_bps=venue.maker_fee_bps,
                taker_fee_bps=venue.taker_fee_bps,
                default_leverage=venue.default_leverage,
                leverages=venue.leverages,
                allow_cash_borrowing=venue.allow_cash_borrowing,
                liquidation_enabled=venue.liquidation_enabled,
                liquidation_trigger_ratio=venue.liquidation_trigger_ratio,
                liquidation_cancel_open_orders=venue.liquidation_cancel_open_orders,
            )
            for venue in request.venues
        ),
        start=request.start,
        end=request.end,
        parameters=request.parameters,
        catalog_path=request.catalog_path,
        runs_path=request.runs_path,
        preset_name=request.preset_name,
    )
    run_id, metrics = execute_local_backtest(
        core_request,
        run_id=run_id,
        report_exporter=partial(export_reports, export_chart=request.export_chart)
        if request.export_artifacts else None,
    )
    LOGGER.info("backtest completed", extra={"event": "backtest.completed", "run_id": run_id, "result": "success", "duration_ms": round((monotonic() - started) * 1000), "params": {"strategy": request.strategy.name, "metrics": metrics, "run_directory": str(request.runs_path / run_id)}})
    return run_id, metrics
