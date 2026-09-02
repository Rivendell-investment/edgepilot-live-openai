from __future__ import annotations

from datetime import datetime
from decimal import Decimal
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Any, Callable

from nautilus_trader.analysis import MaxDrawdown
from nautilus_trader.backtest.config import BacktestDataConfig, BacktestEngineConfig, BacktestRunConfig, BacktestVenueConfig
from nautilus_trader.backtest.node import BacktestNode
from nautilus_trader.config import ImportableStrategyConfig, LoggingConfig
from nautilus_trader.model.data import Bar
from nautilus_trader.persistence.catalog import ParquetDataCatalog

from edgepilot_core.backtest.metrics import collect_metrics
from edgepilot_core.backtest.models import BacktestRequest, MarketRequest, VenueRequest
from edgepilot_core.backtest.presets import public_adapter_options, resolve_strategy_parameters


ReportExporter = Callable[..., None]


def validate_request(request: BacktestRequest) -> None:
    if not request.markets:
        raise ValueError("A backtest requires at least one market")
    names = {venue.name.upper() for venue in request.venues}
    if names != {market.venue.upper() for market in request.markets}:
        raise ValueError("Backtest venues must exactly cover the configured market venues")
    for market in request.markets:
        instrument_venue = market.instrument_id.rsplit(".", 1)[-1].upper()
        if instrument_venue != market.venue.upper():
            raise ValueError(
                f"Market venue mismatch: {market.instrument_id} belongs to {instrument_venue}, "
                f"but is configured under {market.venue.upper()}",
            )
        if market.data_type != "bars":
            raise ValueError("The backtest engine currently requires bar markets")


def execute_local_backtest(
    request: BacktestRequest,
    *,
    run_id: str | None = None,
    report_exporter: ReportExporter | None = None,
) -> tuple[str, dict[str, Any]]:
    """Run only against an existing catalog; network adapters are intentionally absent."""
    validate_request(request)
    run_id = run_id or datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    run_dir = request.runs_path / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    venue_by_name = {venue.name.upper(): venue for venue in request.venues}
    overrides_fees = any(
        venue.maker_fee_bps is not None or venue.taker_fee_bps is not None
        for venue in request.venues
    )
    catalog_path = request.catalog_path
    temporary_catalog = _temporary_catalog_path(run_dir) if overrides_fees else run_dir / ".catalog"
    node: BacktestNode | None = None
    try:
        if overrides_fees:
            _prepare_fee_override_catalog(
                request.catalog_path,
                temporary_catalog,
                {market.instrument_id for market in request.markets},
            )
            catalog_path = temporary_catalog
        fees = {
            market.instrument_id: _resolve_instrument_fees(
                catalog_path, market, venue_by_name[market.venue.upper()],
            )
            for market in request.markets
        }
        parameters = resolve_strategy_parameters(request.strategy, request.parameters)
        run_config = BacktestRunConfig(
            engine=BacktestEngineConfig(
                strategies=[ImportableStrategyConfig(
                    strategy_path=request.strategy.strategy_path,
                    config_path=request.strategy.config_path,
                    config=parameters,
                )],
                logging=LoggingConfig(
                    log_level="ERROR", log_level_file="INFO",
                    log_directory=str(run_dir), log_file_name="nautilus",
                ),
            ),
            venues=[_venue_config(venue) for venue in request.venues],
            data=[BacktestDataConfig(
                catalog_path=str(catalog_path), data_cls=Bar,
                bar_types=[market.bar_type for market in request.markets],
                start_time=request.start.isoformat(), end_time=request.end.isoformat(),
            )],
            start=request.start.isoformat(), end=request.end.isoformat(),
            raise_exception=True, dispose_on_completion=False,
        )
        node = BacktestNode(configs=[run_config])
        node.build()
        engine = node.get_engine(run_config.id)
        if engine is None:
            raise RuntimeError("NautilusTrader did not build the configured backtest engine")
        starting_balance = sum(venue.starting_balance for venue in request.venues)
        engine.portfolio.analyzer.register_statistic(MaxDrawdown())
        node.run()
        metrics = collect_metrics(
            engine, base_currency=request.venues[0].base_currency,
            starting_balance=starting_balance,
            start=request.start, end=request.end,
        )
        record = _run_record(request, run_id, parameters, fees, metrics)
        (run_dir / "run.json").write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
        if report_exporter is not None:
            report_exporter(
                engine, run_dir, metrics, catalog_path=catalog_path,
                bar_types=[market.bar_type for market in request.markets],
                start=request.start, end=request.end, starting_balance=starting_balance,
            )
        return run_id, metrics
    finally:
        if node is not None:
            node.dispose()
        if temporary_catalog.exists():
            shutil.rmtree(temporary_catalog)


# Fee rewrite only mutates instrument parquet. Bar (and similar) data stays shared.
_READ_ONLY_DATA_KINDS = frozenset({"bar"})


def _temporary_catalog_path(run_dir: Path) -> Path:
    """Return a disposable fee overlay path without deep Windows run nesting."""
    if os.name == "nt":
        return Path(tempfile.mkdtemp(prefix="epc-"))
    return run_dir / ".catalog"


def _prepare_fee_override_catalog(
    source: Path,
    target: Path,
    instrument_ids: set[str],
) -> None:
    """Build a tiny writable catalog for fee overrides without copying bar data.

    Known read-only directory kinds (``bar``) are symlinked on POSIX and joined
    on Windows. Every other ``data/*`` entry is copied so
    ``_resolve_instrument_fees`` cannot write through to the shared catalog when
    the instrument layout is unfamiliar.
    """
    # ``tempfile.mkdtemp`` (used on Windows) returns an already-created empty
    # directory.  Do not remove and immediately recreate that directory: on
    # Windows the deletion can be observed asynchronously by the filesystem
    # (or an antivirus/indexer), leaving subsequent ``copytree``/``copy2``
    # calls with WinError 3 even though the source catalog is valid.  A stale
    # POSIX run-local overlay is still removed before rebuilding it.
    if target.is_symlink() or (target.exists() and not target.is_dir()):
        target.unlink()
    elif target.is_dir() and any(target.iterdir()):
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)
    source = source.resolve()
    src_data = source / "data"
    if not src_data.is_dir():
        raise RuntimeError(f"Catalog data directory missing: {src_data}")
    dst_data = target / "data"
    dst_data.mkdir(parents=True, exist_ok=True)

    for child in src_data.iterdir():
        dest = dst_data / child.name
        if child.name in _READ_ONLY_DATA_KINDS:
            if os.name == "nt" and child.is_dir():
                _create_windows_junction(child.resolve(), dest)
            else:
                dest.symlink_to(child.resolve(), target_is_directory=child.is_dir())
            continue
        _copy_writable_data_kind(child, dest, instrument_ids)

    for child in source.iterdir():
        if child.name == "data":
            continue
        dest = target / child.name
        if child.is_dir():
            shutil.copytree(child, dest, symlinks=False)
        elif child.is_file() or child.is_symlink():
            shutil.copy2(child, dest, follow_symlinks=True)


def _create_windows_junction(source: Path, target: Path) -> None:
    """Create a directory junction without requiring symlink privileges."""
    try:
        subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(target), str(source)],
            check=True,
            capture_output=True,
            text=True,
            # Backtests run under the console-less local service; without this
            # every junction would flash a cmd.exe window at the user.
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", None) or str(exc)
        raise RuntimeError(f"Could not create bar catalog junction {target}: {detail.strip()}") from exc


def _copy_writable_data_kind(src: Path, dest: Path, instrument_ids: set[str]) -> None:
    """Copy instrument metadata. Never symlink — fee rewrite deletes/writes parquet here."""
    if src.is_file() or (src.is_symlink() and not src.is_dir()):
        shutil.copy2(src, dest, follow_symlinks=True)
        return
    dest.mkdir(parents=True, exist_ok=True)
    selected = False
    for instrument_id in instrument_ids:
        src_instrument = src / instrument_id
        if src_instrument.is_dir():
            shutil.copytree(src_instrument, dest / instrument_id, symlinks=False)
            selected = True
        elif src_instrument.is_file():
            shutil.copy2(src_instrument, dest / instrument_id)
            selected = True
    if selected:
        return
    shutil.rmtree(dest)
    shutil.copytree(src, dest, symlinks=False)


def _venue_config(venue: VenueRequest) -> BacktestVenueConfig:
    return BacktestVenueConfig(
        name=venue.name, oms_type=venue.oms_type,
        account_type=venue.account_type if venue.account_type.upper() in {"CASH", "MARGIN"} else "MARGIN",
        base_currency=venue.base_currency,
        starting_balances=[f"{venue.starting_balance} {venue.base_currency}"],
        default_leverage=venue.default_leverage, leverages=venue.leverages,
        allow_cash_borrowing=venue.allow_cash_borrowing,
        liquidation_enabled=venue.liquidation_enabled,
        liquidation_trigger_ratio=venue.liquidation_trigger_ratio,
        liquidation_cancel_open_orders=venue.liquidation_cancel_open_orders,
        bar_adaptive_high_low_ordering=True,
    )


def _run_record(
    request: BacktestRequest,
    run_id: str,
    parameters: dict[str, Any],
    fees: dict[str, tuple[float, float]],
    metrics: dict[str, Any],
) -> dict[str, Any]:
    return {
        "run_id": run_id, "mode": "backtest",
        "strategy": {
            "name": request.strategy.name, "strategy_path": request.strategy.strategy_path,
            "config_path": request.strategy.config_path, "preset": request.preset_name,
            "parameters": parameters,
        },
        "markets": [market.__dict__ | {"venue": market.venue.upper()} for market in request.markets],
        "venues": [
            {
                "adapter": venue.name, "adapter_options": public_adapter_options(venue.adapter_options),
                "starting_balance": venue.starting_balance, "base_currency": venue.base_currency,
                "account_type": venue.account_type, "oms_type": venue.oms_type,
                "maker_fee_bps": fees[next(m.instrument_id for m in request.markets if m.venue.upper() == venue.name.upper())][0],
                "taker_fee_bps": fees[next(m.instrument_id for m in request.markets if m.venue.upper() == venue.name.upper())][1],
                "default_leverage": venue.default_leverage, "leverages": venue.leverages,
                "allow_cash_borrowing": venue.allow_cash_borrowing,
                "liquidation_enabled": venue.liquidation_enabled,
                "liquidation_trigger_ratio": venue.liquidation_trigger_ratio,
                "liquidation_cancel_open_orders": venue.liquidation_cancel_open_orders,
            } for venue in request.venues
        ],
        "period": {"start": request.start.isoformat(), "end": request.end.isoformat()},
        "metrics": metrics,
    }


def _resolve_instrument_fees(
    catalog_path: Path, market: MarketRequest, venue: VenueRequest,
) -> tuple[float, float]:
    catalog = ParquetDataCatalog(str(catalog_path))
    instruments = catalog.instruments(instrument_ids=[market.instrument_id])
    if not instruments:
        raise RuntimeError(f"Instrument unavailable in catalog: {market.instrument_id}")
    instrument = instruments[-1]
    native_maker_bps = float(instrument.maker_fee) * 10_000
    native_taker_bps = float(instrument.taker_fee) * 10_000
    maker = venue.maker_fee_bps if venue.maker_fee_bps is not None else native_maker_bps
    taker = venue.taker_fee_bps if venue.taker_fee_bps is not None else native_taker_bps
    if venue.maker_fee_bps is None and venue.taker_fee_bps is None:
        return maker, taker
    values = type(instrument).to_dict(instrument)
    values["maker_fee"] = str(Decimal(str(maker)) / Decimal("10000"))
    values["taker_fee"] = str(Decimal(str(taker)) / Decimal("10000"))
    now_ns = int(datetime.now().timestamp() * 1_000_000_000)
    values["ts_event"] = values["ts_init"] = now_ns
    files = catalog.filter_files(
        type(instrument), catalog.get_file_list_from_data_cls(type(instrument)),
        identifiers=[market.instrument_id],
    )
    for file_path in files:
        catalog.fs.rm(file_path)
    catalog.write_data([type(instrument).from_dict(values)])
    return maker, taker
