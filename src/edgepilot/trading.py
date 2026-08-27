from __future__ import annotations

import json
import logging
import signal
from datetime import datetime, timezone
from pathlib import Path
import sys
from threading import Event
from threading import Lock
from threading import Thread
from typing import Any

from nautilus_trader.common.config import resolve_path
from nautilus_trader.config import ImportableStrategyConfig
from nautilus_trader.config import LiveDataEngineConfig
from nautilus_trader.config import LoggingConfig
from nautilus_trader.config import TradingNodeConfig
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.events.position import PositionClosed
from nautilus_trader.model.identifiers import Venue

from edgepilot.discovery import AdapterDescriptor
from edgepilot.discovery import resolve_adapter
from edgepilot.discovery import instantiate_config
from edgepilot.environment import credential_options
from edgepilot.environment import credential_requirements
from edgepilot.paths import state_root
from edgepilot.paths import strategy_runs_path
from edgepilot.run_state import EMERGENCY_STOP_FILE
from edgepilot.run_state import RUNTIME_FILE
from edgepilot.run_state import register_running
from edgepilot.run_state import request_emergency_stop
from edgepilot.run_state import running_runs
from edgepilot.run_state import unregister_running


LOGGER = logging.getLogger("edgepilot.trading")


class _UnexpectedShutdown:
    """Capture engine-initiated shutdowns which otherwise return exit code zero."""

    def __init__(self) -> None:
        self.reason: str | None = None

    def __call__(self, command: Any) -> None:
        self.reason = str(command.reason or "Trading engine requested shutdown")

    def raise_if_requested(self) -> None:
        if self.reason is not None:
            raise RuntimeError(f"Trading engine stopped unexpectedly: {self.reason}")


SANDBOX_EXEC_CONFIG = (
    "nautilus_trader.adapters.sandbox.config:SandboxExecutionClientConfig"
)
SANDBOX_EXEC_FACTORY = (
    "nautilus_trader.adapters.sandbox.factory:SandboxLiveExecClientFactory"
)


def _frame_rows(frame: Any, *, limit: int = 100) -> list[dict[str, Any]]:
    """Convert a native Nautilus report to a small JSON-safe browser payload."""
    if frame is None or frame.empty:
        return []
    # Pandas' JSON encoder handles Nautilus values, timestamps, and Decimal-like
    # objects more consistently than converting report rows directly.
    return json.loads(frame.tail(limit).to_json(orient="records", date_format="iso"))


class _RunPnlLedger:
    """Accumulate native close events for one local trading run."""

    def __init__(self) -> None:
        self._totals: dict[str, float] = {}
        self._lock = Lock()

    def handle(self, event: Any) -> None:
        if not isinstance(event, PositionClosed):
            return
        with self._lock:
            currency = str(event.realized_pnl.currency)
            self._totals[currency] = (
                self._totals.get(currency, 0.0) + event.realized_pnl.as_double()
            )

    def totals(self, cache: Any) -> dict[str, float]:
        """Combine completed cycles with native P&L on currently open positions."""
        totals: dict[str, float] = {}
        with self._lock:
            totals.update(self._totals)
        for position in cache.positions_open():
            money = position.realized_pnl
            currency = str(money.currency)
            totals[currency] = totals.get(currency, 0.0) + money.as_double()
        return totals


def _write_runtime_snapshot(
    node: TradingNode,
    record: dict[str, Any],
    mode: str,
    pnl_ledger: _RunPnlLedger,
) -> None:
    """Persist read-only native reports for the local dashboard.

    This intentionally reads the TradingNode's existing reports. It does not
    query, alter, or reconcile exchange state outside Nautilus.
    """
    accounts: dict[str, list[dict[str, Any]]] = {}
    errors: list[str] = []
    for venue_record in record["venues"]:
        venue_name = str(venue_record["adapter"])
        try:
            accounts[venue_name] = _frame_rows(
                node.trader.generate_account_report(venue=Venue(venue_name)),
                limit=20,
            )
        except Exception as exc:  # An account may not be available during startup.
            accounts[venue_name] = []
            errors.append(f"{venue_name}: {exc}")
    reports: dict[str, Any] = {}
    for key, generate in (
        ("orders", node.trader.generate_orders_report),
        ("fills", node.trader.generate_fills_report),
        ("positions", node.trader.generate_positions_report),
    ):
        try:
            reports[key] = _frame_rows(generate())
        except Exception as exc:  # One unavailable report must not erase the others.
            reports[key] = []
            errors.append(f"{key}: {exc}")
    orders, fills, positions = reports["orders"], reports["fills"], reports["positions"]
    try:
        realized_pnl = pnl_ledger.totals(node.cache)
    except Exception as exc:
        realized_pnl = {}
        errors.append(f"realized_pnl: {exc}")
    payload = {
        "run_id": record["run_id"],
        "mode": mode,
        "status": "RUNNING",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "accounts": accounts,
        "orders": orders,
        "fills": fills,
        "positions": positions,
        "realized_pnl": realized_pnl,
        "errors": errors,
    }
    directory = strategy_runs_path(str(record["strategy"]["name"])) / record["run_id"]
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / RUNTIME_FILE
    temporary = directory / f".{RUNTIME_FILE}.tmp"
    temporary.write_text(json.dumps(payload, separators=(",", ":"), default=str), encoding="utf-8")
    temporary.replace(target)


def _mark_runtime_stopped(record: dict[str, Any], *, failed: bool = False) -> None:
    """Leave the last native snapshot visible, but accurately mark it stopped."""
    directory = strategy_runs_path(str(record["strategy"]["name"])) / record["run_id"]
    path = directory / RUNTIME_FILE
    payload: dict[str, Any] = {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    payload.update({
        "run_id": record["run_id"],
        "mode": payload.get("mode", record.get("mode")),
        "status": "FAILED" if failed else "STOPPED",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })
    directory.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, separators=(",", ":"), default=str), encoding="utf-8")


def _start_runtime_snapshots(
    node: TradingNode,
    record: dict[str, Any],
    mode: str,
    pnl_ledger: _RunPnlLedger,
) -> Event:
    stop = Event()

    def publish() -> None:
        failure_logged = False
        while not stop.is_set():
            try:
                _write_runtime_snapshot(node, record, mode, pnl_ledger)
                failure_logged = False
            except Exception:
                # The node's own log remains the source of diagnostic detail.
                # A transient dashboard write must not affect trading.
                if not failure_logged:
                    LOGGER.exception(
                        "runtime snapshot failed",
                        extra={
                            "event": "trading.runtime_snapshot.failed",
                            "run_id": record.get("run_id"),
                            "result": "failed",
                        },
                    )
                    failure_logged = True
            stop.wait(2.0)

    Thread(target=publish, name=f"edgepilot-runtime-{record['run_id']}", daemon=True).start()
    return stop


def _watch_emergency_stop(node: TradingNode, record: dict[str, Any]) -> tuple[Event, Path]:
    """Watch the run-local stop request from the dashboard on every platform."""
    stop = Event()
    request_path = strategy_runs_path(str(record["strategy"]["name"])) / record["run_id"] / EMERGENCY_STOP_FILE
    request_path.unlink(missing_ok=True)

    def watch() -> None:
        while not stop.wait(0.25):
            if request_path.exists():
                node.stop()
                return

    Thread(target=watch, name=f"edgepilot-emergency-stop-{record['run_id']}", daemon=True).start()
    return stop, request_path


def execute_trading(
    *,
    record: dict[str, Any],
    mode: str,
    dry_run: bool,
) -> None:
    LOGGER.info("trading configuration started", extra={"event": "trading.configure.started", "run_id": record.get("run_id"), "params": {"mode": mode, "dry_run": dry_run, "strategy": record.get("strategy", {}).get("name"), "markets": record.get("markets", []), "venues": record.get("venues", [])}})
    # User-installed strategies always win over a development/plugin checkout.
    # A child Demo/Live process must be able to import ``strategies.NAME``
    # without relying on the terminal's working directory.
    strategy_parent = str(state_root())
    if strategy_parent in sys.path:
        sys.path.remove(strategy_parent)
    sys.path.insert(0, strategy_parent)

    if mode not in {"paper", "demo", "live"}:
        raise ValueError(f"Unsupported trading mode: {mode}")
    strategy = record["strategy"]
    markets = record["markets"]
    venue_records = record["venues"]
    data_clients = {}
    exec_clients = {}
    factories = {}
    missing_credentials: dict[str, list[str]] = {}
    execution = "LOCAL_SANDBOX" if mode == "paper" else ("EXCHANGE_DEMO" if mode == "demo" else "EXCHANGE_LIVE")
    for venue_record in venue_records:
        adapter = resolve_adapter(venue_record["adapter"])
        venue_markets = [market for market in markets if market["venue"].upper() == adapter.name]
        if not venue_markets:
            raise ValueError(f"No markets configured for venue {adapter.name}")
        if mode != "paper" and (adapter.exec_config_path is None or adapter.exec_factory_path is None):
            raise ValueError(f"Nautilus adapter {adapter.name} does not expose execution support")
        requirements = credential_requirements(adapter, mode)
        missing = [
            item["environment_variable"]
            for item in requirements
            if mode != "paper" and not item["configured"]
        ]
        if missing:
            missing_credentials[adapter.name] = missing
        data_options = dict(venue_record.get("adapter_options", {}))
        # Keep the generic venue account selection visible to Nautilus' native
        # adapter.  Binance uses this to distinguish Spot from USD-M/COIN-M
        # Futures; dropping it silently makes a perpetual preset open Spot.
        if str(venue_record["account_type"]).upper() != "MARGIN":
            data_options.setdefault("account_type", venue_record["account_type"])
        data_options.setdefault("environment", "LIVE" if mode == "paper" else mode.upper())
        data_options.setdefault(
            "instrument_provider",
            {"load_all": False, "load_ids": [market["instrument_id"] for market in venue_markets]},
        )
        for field, value in credential_options(adapter, mode, adapter.data_config_path).items():
            data_options.setdefault(field, value)
        data_clients[adapter.name] = instantiate_config(adapter.data_config_path, data_options)
        if mode == "paper":
            exec_config_path = SANDBOX_EXEC_CONFIG
            exec_factory_path = SANDBOX_EXEC_FACTORY
            sandbox_account_type = (
                venue_record["account_type"]
                if str(venue_record["account_type"]).upper() in {"CASH", "MARGIN"}
                else "MARGIN"
            )
            exec_clients[adapter.name] = instantiate_config(
                exec_config_path,
                {
                    "venue": adapter.name,
                    # The native sandbox is a second Nautilus client.  It must
                    # load the exact same instruments as the live data client
                    # before it can simulate fills from incoming bars.
                    "instrument_provider": {
                        "load_all": False,
                        "load_ids": [market["instrument_id"] for market in venue_markets],
                    },
                    "starting_balances": [
                        f"{venue_record['starting_balance']} {venue_record['base_currency']}",
                    ],
                    "base_currency": venue_record["base_currency"],
                    "oms_type": venue_record["oms_type"],
                    # Venue-specific values such as Binance's USDT_FUTURES are
                    # valid for the data adapter, but the local Nautilus
                    # sandbox uses its generic cash/margin account model.
                    "account_type": sandbox_account_type,
                },
            )
        else:
            exec_config_path = adapter.exec_config_path
            exec_factory_path = adapter.exec_factory_path
            exec_options = dict(data_options)
            for field, value in credential_options(adapter, mode, exec_config_path).items():
                exec_options.setdefault(field, value)
            exec_clients[adapter.name] = instantiate_config(exec_config_path, exec_options)
        factories[adapter.name] = (adapter.data_factory_path, exec_factory_path)
    if missing_credentials and not dry_run:
        details = "; ".join(f"{venue}: {', '.join(values)}" for venue, values in missing_credentials.items())
        raise ValueError(f"Missing {mode} credentials: {details}")
    strategy_config = ImportableStrategyConfig(
        strategy_path=strategy["strategy_path"],
        config_path=strategy["config_path"],
        config=strategy["parameters"],
    )
    config = TradingNodeConfig(
        strategies=[strategy_config],
        data_clients=data_clients,
        exec_clients=exec_clients,
        # Default (False) calls os._exit(1) on queue exceptions, bypassing Python's
        # exception handler and leaving no traceable error in the job output.
        data_engine=LiveDataEngineConfig(graceful_shutdown_on_exception=True),
        logging=LoggingConfig(
            log_level="INFO",
            log_level_file="DEBUG",
            log_directory=str(strategy_runs_path(str(record["strategy"]["name"])) / record["run_id"]),
            log_file_name="nautilus",
            use_pyo3=True,
        ),
    )
    if dry_run:
        print(
            json.dumps(
                {
                    "mode": mode,
                    "execution": execution,
                    "adapters": [venue["adapter"] for venue in venue_records],
                    "markets": markets,
                    "strategy_path": strategy["strategy_path"],
                    "config_path": strategy["config_path"],
                    "parameters": strategy["parameters"],
                    "exchange_orders": mode != "paper",
                    "execution_credentials_required": mode != "paper",
                    "market_data_credentials_required_by_wrapper": False,
                    "credential_variables_supported": [
                        item["environment_variable"] for item in requirements
                    ],
                    "credentials_configured": not missing_credentials,
                },
                indent=2,
            ),
        )
        LOGGER.info("trading dry run completed", extra={"event": "trading.dry_run.completed", "run_id": record.get("run_id"), "result": "success", "params": {"mode": mode, "credentials_configured": not missing_credentials}})
        return

    node = TradingNode(config=config)
    for name, (data_factory_path, exec_factory_path) in factories.items():
        node.add_data_client_factory(name, resolve_path(data_factory_path))
        node.add_exec_client_factory(name, resolve_path(exec_factory_path))
    node.build()
    unexpected_shutdown = _UnexpectedShutdown()
    node.kernel.msgbus.subscribe("commands.system.shutdown", unexpected_shutdown)
    LOGGER.info("trading node built", extra={"event": "trading.node.built", "run_id": record.get("run_id"), "result": "success", "params": {"mode": mode, "execution": execution}})
    pnl_ledger = _RunPnlLedger()
    node.kernel.msgbus.subscribe("events.position.*", pnl_ledger.handle)
    runtime_stop = _start_runtime_snapshots(node, record, mode, pnl_ledger)
    emergency_watch_stop, emergency_request_path = _watch_emergency_stop(node, record)
    previous_sigterm = signal.getsignal(signal.SIGTERM)

    def emergency_stop(_signum: int, _frame: Any) -> None:
        """Stop through Nautilus so every strategy receives ``on_stop``."""
        node.stop()

    signal.signal(signal.SIGTERM, emergency_stop)
    failed = False
    try:
        LOGGER.info("trading node started", extra={"event": "trading.node.started", "run_id": record.get("run_id"), "params": {"mode": mode}})
        node.run()
        unexpected_shutdown.raise_if_requested()
    except Exception:
        failed = True
        LOGGER.exception("trading node failed", extra={"event": "trading.node.failed", "run_id": record.get("run_id"), "result": "failed", "params": {"mode": mode}})
        raise
    finally:
        runtime_stop.set()
        emergency_watch_stop.set()
        emergency_request_path.unlink(missing_ok=True)
        signal.signal(signal.SIGTERM, previous_sigterm)
        node.dispose()
        _mark_runtime_stopped(record, failed=failed)
        LOGGER.info("trading node stopped", extra={"event": "trading.node.stopped", "run_id": record.get("run_id"), "result": "failed" if failed else "success", "params": {"mode": mode}})
