from __future__ import annotations

import argparse
from datetime import datetime
from datetime import timedelta
from datetime import timezone
import json
import hashlib
import importlib
import inspect as python_inspect
import logging
from pathlib import Path
import shutil
import sys
from time import monotonic
from typing import Any
from typing import Callable

from edgepilot import __version__
from edgepilot.env_file import load_env
from edgepilot.paths import account_credentials_path
from edgepilot.paths import active_account_key
from edgepilot.paths import clear_bound_account_key
from edgepilot.paths import find_run_directory
from edgepilot.paths import iter_run_directories
from edgepilot.paths import state_root
from edgepilot.paths import strategy_runs_path
from edgepilot import marketplace
from edgepilot.run_state import load_run
from edgepilot.run_state import load_execution
from edgepilot.run_state import record_execution_result
from edgepilot.run_state import register_running
from edgepilot.run_state import request_emergency_stop
from edgepilot.run_state import running_runs
from edgepilot.run_state import run_status
from edgepilot.run_state import unregister_running
from edgepilot.values import parse_assignments
from edgepilot.app_logging import configure_logging
from edgepilot import auth


UTC = timezone.utc
STATE_ROOT = state_root()
CATALOG = STATE_ROOT / "catalog"


def _lazy_callable(module_name: str, attribute: str) -> Callable[..., Any]:
    """Delay native imports until a command actually needs them."""
    def invoke(*args: Any, **kwargs: Any) -> Any:
        target = getattr(importlib.import_module(module_name), attribute)
        return target(*args, **kwargs)

    return invoke


execute_backtest = _lazy_callable("edgepilot.backtest", "execute_backtest")
parse_time = _lazy_callable("edgepilot.catalog", "parse_time")
pull_data = _lazy_callable("edgepilot.catalog", "pull_data")
resolve_adapter = _lazy_callable("edgepilot.discovery", "resolve_adapter")
resolve_strategy = _lazy_callable("edgepilot.discovery", "resolve_strategy")
strategies_root = _lazy_callable("edgepilot.discovery", "strategies_root")
strategy_names = _lazy_callable("edgepilot.discovery", "strategy_names")
credential_requirements = _lazy_callable("edgepilot.environment", "credential_requirements")
load_preset = _lazy_callable("edgepilot.presets", "load_preset")
preset_backtest_values = _lazy_callable("edgepilot.presets", "preset_backtest_values")
preset_markets = _lazy_callable("edgepilot.presets", "preset_markets")
preset_names = _lazy_callable("edgepilot.presets", "preset_names")
preset_strategy_values = _lazy_callable("edgepilot.presets", "preset_strategy_values")
preset_venues = _lazy_callable("edgepilot.presets", "preset_venues")
public_adapter_options = _lazy_callable("edgepilot.presets", "public_adapter_options")
resolve_strategy_parameters = _lazy_callable("edgepilot.presets", "resolve_strategy_parameters")
execute_trading = _lazy_callable("edgepilot.trading", "execute_trading")


def _period(args: argparse.Namespace, *, default_days: int = 365) -> tuple[datetime, datetime]:
    end = parse_time(args.end) if args.end else datetime.now(UTC)
    if args.start:
        start = parse_time(args.start)
    else:
        start = end - timedelta(days=args.days if args.days is not None else default_days)
    if start >= end:
        raise ValueError("start must be earlier than end")
    return start, end


def _full_bar_type(instrument: str, value: str) -> str:
    return value if value.startswith(f"{instrument}-") else f"{instrument}-{value}"


def _add_period(parser: argparse.ArgumentParser, *, default_days: int | None = 365) -> None:
    parser.add_argument("--start", help="ISO-8601 UTC start time")
    parser.add_argument("--end", help="ISO-8601 UTC end time")
    parser.add_argument(
        "--days",
        type=int,
        default=default_days,
        help="Rolling period before --end; overridden by --start",
    )


def _add_adapter_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--adapter-set",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Native adapter config value; repeat as needed",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="edgepilot", description="EdgePilot trading strategy CLI")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    strategies = subparsers.add_parser("strategies", help="List strategies, presets, and settings")
    strategies.add_argument(
        "action",
        choices=["list", "inspect", "presets", "remove"],
        nargs="?",
        default="list",
        help="Operation to perform (default: list)",
    )
    strategies.add_argument("name", nargs="?", help="Local strategy name")
    strategies.add_argument("--confirm", action="store_true", help="Required to remove a local strategy package")

    data = subparsers.add_parser("data", help="Download native historical data into the catalog")
    data.add_argument("action", choices=["pull"], help="Download into the local catalog")
    data.add_argument("--venue", required=True, help="Native adapter name, such as OKX or BINANCE")
    data.add_argument("--instrument", required=True, help="Full Nautilus instrument ID")
    data.add_argument(
        "--data-type",
        choices=["bars", "trades", "quotes", "order-book-depth", "order-book-deltas"],
        default="bars",
        help="Native market data type (default: bars)",
    )
    data.add_argument(
        "--bar-type",
        default="1-HOUR-LAST-EXTERNAL",
        help="Bar specification or full bar type (default: 1-HOUR-LAST-EXTERNAL)",
    )
    _add_period(data)
    _add_adapter_options(data)

    backtest = subparsers.add_parser("backtest", help="Backtest an installed strategy")
    backtest.add_argument("strategy", help="Installed strategy name or import path")
    backtest.add_argument("--config-path", help="Explicit StrategyConfig import path")
    backtest.add_argument("--preset", help="Named strategy configuration; defaults to 'default'")
    backtest.add_argument(
        "--set",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Override a native strategy setting; repeat as needed",
    )
    _add_period(backtest, default_days=None)

    mode_help = {
        "paper": "Run with local Nautilus simulated execution",
        "demo": "Run against the exchange demo/test environment",
        "live": "Run against the exchange live environment",
    }
    for mode in ("paper", "demo", "live"):
        trading = subparsers.add_parser(mode, help=mode_help[mode])
        source = trading.add_mutually_exclusive_group(required=True)
        source.add_argument("--run", dest="run_id", help="Reuse an exact saved run configuration")
        source.add_argument("--strategy", help="Start directly from an installed strategy")
        trading.add_argument("--preset", help="Named strategy configuration; defaults to 'default'")
        trading.add_argument("--venue", help="Run only the market configured for this venue")
        trading.add_argument("--dry-run", action="store_true", help="Validate without connecting")
        if mode == "live":
            trading.add_argument("--confirm-live", action="store_true", help="Required before real orders are enabled")

    runs = subparsers.add_parser("runs", help="Inspect saved runs or emergency-stop an active trading run")
    runs.add_argument(
        "action",
        choices=["list", "show", "status", "emergency-list", "emergency-stop"],
        nargs="?",
        default="list",
        help="Operation to perform (default: list)",
    )
    runs.add_argument("run_id", nargs="?", help="Saved run ID")

    credentials = subparsers.add_parser(
        "credentials",
        help="Check credentials required by a saved run or native adapter",
    )
    credentials.add_argument("action", choices=["check"], nargs="?", default="check")
    credentials.add_argument(
        "--mode",
        choices=["paper", "demo", "live"],
        default="live",
        help="Credential namespace to inspect (default: live)",
    )
    target = credentials.add_mutually_exclusive_group(required=True)
    target.add_argument("--run", dest="run_id", help="Infer the adapter from a saved run")
    target.add_argument("--venue", help="Inspect a native adapter directly")
    target.add_argument("--strategy", help="Infer the adapter from a strategy preset")
    credentials.add_argument("--preset", help="Preset used with --strategy; defaults to 'default'")

    marketplace_parser = subparsers.add_parser("marketplace", help="Search and install public marketplace strategies")
    marketplace_parser.add_argument("action", choices=["search", "inspect", "install", "history", "restore", "clear"])
    marketplace_parser.add_argument("query", nargs="?", default="", help="Full-text search terms for search")
    marketplace_parser.add_argument("--asset", default="", help="Filter by asset or instrument")
    marketplace_parser.add_argument("--venue", default="", help="Filter by venue")
    marketplace_parser.add_argument("--category", default="", help="Filter by category")
    marketplace_parser.add_argument("--data-type", default="", help="Filter by data type")
    marketplace_parser.add_argument("--risk-profile", choices=["conservative", "balanced", "aggressive", "稳健", "平衡", "激进"], help="Risk profile: Conservative/Balanced/Aggressive or 稳健/平衡/激进")
    marketplace_parser.add_argument("--min-capacity-usd", type=float, help="Minimum publisher-declared USD capacity")
    marketplace_parser.add_argument("--sort", choices=["published", "return", "drawdown", "sharpe"], default="published")
    marketplace_parser.add_argument("--version", help="Exact Marketplace version required for inspect and install")
    marketplace_parser.add_argument("--strategy", help="Strategy slug for restore or cloud-history clear")
    marketplace_parser.add_argument("--locale", default="", help="Marketplace response locale for search and inspect (for example zh-CN)")

    auth_parser = subparsers.add_parser("auth", help="Log in, inspect, or revoke the EdgePilot session")
    auth_parser.add_argument("action", choices=["login", "status", "logout"])
    auth_parser.add_argument("--all", action="store_true", help="Revoke all devices")
    auth_parser.add_argument("--local-only", action="store_true", help="Only remove local credentials")

    ui = subparsers.add_parser("ui", help="Open the local EdgePilot dashboard")
    ui.add_argument("--host", default="127.0.0.1", help="Bind address (default: localhost only)")
    ui.add_argument("--port", type=int, default=8787, help="Dashboard port (default: 8787)")
    ui.add_argument("--language", "--lang", help="Dashboard language candidate (for example zh-CN)")
    return parser


def _strategies(args: argparse.Namespace) -> int:
    if args.action == "list":
        for name in strategy_names():
            print(name)
        return 0
    if not args.name:
        raise ValueError(f"strategy name is required for {args.action}")
    if args.action == "remove":
        _preflight_strategy_removal(args.name, args.confirm)
        with marketplace.strategy_operation_lock(args.name):
            package = strategies_root() / args.name
            runs_path = package / "runs"
            active = running_runs(runs_path) if runs_path.exists() else {}
            if active:
                raise ValueError("stop active runs before removing this strategy: " + ", ".join(sorted(active)))
            if package.is_dir():
                shutil.rmtree(package)
            else:
                package.unlink()
        print(json.dumps({"removed": args.name}, indent=2))
        return 0
    descriptor = resolve_strategy(args.name)
    available_presets = preset_names(descriptor)
    if args.action == "presets":
        print(json.dumps({"strategy": descriptor.name, "presets": available_presets}, indent=2))
        return 0
    _, default_preset = load_preset(descriptor, None)
    print(
        json.dumps(
            {
                "name": descriptor.name,
                "strategy_path": descriptor.strategy_path,
                "config_path": descriptor.config_path,
                "presets": available_presets,
                "default_preset": default_preset or None,
                "config_schema": descriptor.config_cls.json_schema(),
            },
            indent=2,
            default=str,
        ),
    )
    return 0


def _preflight_strategy_removal(name: str | None, confirmed: bool) -> None:
    if not confirmed:
        raise ValueError("strategy removal requires --confirm")
    if not name or name not in strategy_names():
        raise ValueError(f"unknown local strategy: {name or ''}")
    package = strategies_root() / name
    runs_path = package / "runs"
    active = running_runs(runs_path) if runs_path.exists() else {}
    if active:
        raise ValueError("stop active runs before removing this strategy: " + ", ".join(sorted(active)))


def _data(args: argparse.Namespace) -> int:
    start, end = _period(args)
    bar_type = _full_bar_type(args.instrument, args.bar_type) if args.data_type == "bars" else None
    pull_data(
        catalog_path=CATALOG,
        adapter=resolve_adapter(args.venue),
        instrument_id=args.instrument,
        data_type=args.data_type,
        start=start,
        end=end,
        bar_type=bar_type,
        adapter_options=parse_assignments(args.adapter_set),
    )
    print(f"Catalog updated: {CATALOG}")
    return 0


def _run_markets(preset: dict) -> tuple[Any, ...]:
    from edgepilot.models import MarketRequest

    return tuple(
        MarketRequest(
            instrument_id=str(item["instrument_id"]),
            bar_type=str(item["bar_type"]),
            venue=str(item["venue"]).upper(),
            data_type=str(item.get("data_type", "bars")),
        )
        for item in preset_markets(preset)
    )


def _run_venues(preset: dict) -> tuple[Any, ...]:
    from edgepilot.models import VenueRequest

    venues = []
    reserved = {
        "starting_balance",
        "base_currency",
        "account_type",
        "oms_type",
        "maker_fee_bps",
        "taker_fee_bps",
        "default_leverage",
        "leverages",
        "allow_cash_borrowing",
        "liquidation_enabled",
        "liquidation_trigger_ratio",
        "liquidation_cancel_open_orders",
    }
    for name, settings in preset_venues(preset).items():
        venues.append(
            VenueRequest(
                adapter=resolve_adapter(name),
                adapter_options={key: value for key, value in settings.items() if key not in reserved},
                starting_balance=float(settings.get("starting_balance", 100_000.0)),
                base_currency=str(settings.get("base_currency", "USDT")),
                account_type=str(settings.get("account_type", "MARGIN")),
                oms_type=str(settings.get("oms_type", "NETTING")),
                maker_fee_bps=settings.get("maker_fee_bps"),
                taker_fee_bps=settings.get("taker_fee_bps"),
                default_leverage=float(settings.get("default_leverage", 1.0)),
                leverages=settings.get("leverages"),
                allow_cash_borrowing=bool(settings.get("allow_cash_borrowing", False)),
                liquidation_enabled=bool(settings.get("liquidation_enabled", False)),
                liquidation_trigger_ratio=float(settings.get("liquidation_trigger_ratio", 1.0)),
                liquidation_cancel_open_orders=bool(settings.get("liquidation_cancel_open_orders", True)),
            ),
        )
    return tuple(venues)


def _backtest(args: argparse.Namespace) -> int:
    from edgepilot.backtest import BacktestRequest

    strategy = resolve_strategy(args.strategy, args.config_path)
    preset_name, preset = load_preset(strategy, args.preset)
    backtest_values = preset_backtest_values(preset)
    start, end = _period(args, default_days=int(backtest_values.get("days", 365)))
    strategy_values = preset_strategy_values(preset)
    strategy_values.update(parse_assignments(args.set))
    markets = _run_markets(preset)
    venues = _run_venues(preset)
    run_id, metrics = execute_backtest(
        BacktestRequest(
            strategy=strategy,
            markets=markets,
            venues=venues,
            start=start,
            end=end,
            parameters=strategy_values,
            catalog_path=CATALOG,
            runs_path=strategy_runs_path(strategy.name),
            # Historical backtests always fill missing catalog data.  Keep
            # accepting legacy presets which still contain backtest.download,
            # but do not let that obsolete field disable data preparation.
            download=True,
            export_artifacts=bool(backtest_values.get("export_artifacts", True)),
            preset_name=preset_name,
        ),
    )
    print(json.dumps({"run_id": run_id, "metrics": metrics}, indent=2))
    print(f"Chart: {strategy_runs_path(strategy.name) / run_id / 'backtest.png'}")
    return 0


def _select_trading_venue(record: dict[str, Any], requested_venue: str) -> None:
    venue = requested_venue.strip().upper()
    configured = {
        str(item.get("adapter", "")).strip().upper(): item
        for item in record.get("venues", [])
        if isinstance(item, dict)
    }
    if venue not in configured:
        raise ValueError(f"Venue is not configured by this preset: {venue}")
    markets = [
        market
        for market in record.get("markets", [])
        if isinstance(market, dict) and str(market.get("venue", "")).strip().upper() == venue
    ]
    if not markets:
        raise ValueError(f"Selected venue has no configured markets: {venue}")

    market_instruments = {
        str(market.get("instrument_id", "")).strip()
        for market in markets
        if str(market.get("instrument_id", "")).strip()
    }
    strategy_instruments: set[str] = set()
    parameters = record.get("strategy", {}).get("parameters", {})
    if isinstance(parameters, dict):
        for key, value in parameters.items():
            if key.endswith("instrument_id") and isinstance(value, str) and value.strip():
                strategy_instruments.add(value.strip())
            elif key.endswith("instrument_ids") and isinstance(value, list):
                strategy_instruments.update(str(item).strip() for item in value if isinstance(item, str) and item.strip())
    mismatched = strategy_instruments - market_instruments
    if mismatched:
        raise ValueError(
            f"Selected venue does not contain strategy instruments: {', '.join(sorted(mismatched))}",
        )

    record["markets"] = markets
    record["venues"] = [configured[venue]]


def _trade(args: argparse.Namespace, mode: str) -> int:
    if mode == "live" and not args.confirm_live and not args.dry_run:
        raise PermissionError("Live trading requires --confirm-live")
    if args.run_id and args.venue is not None:
        raise ValueError("Venue selection requires a strategy preset, not an exact saved run")
    if args.run_id:
        runs_path = find_run_directory(args.run_id).parent
        record = load_run(runs_path, args.run_id)
        run_id = args.run_id
        strategy = resolve_strategy(str(record.get("strategy", {}).get("name", "")))
    else:
        strategy = resolve_strategy(args.strategy)
        preset_name, preset = load_preset(strategy, args.preset)
        strategy_values = preset_strategy_values(preset)
        resolved = resolve_strategy_parameters(strategy, strategy_values)
        markets = _run_markets(preset)
        venues = _run_venues(preset)
        run_id = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        record = {
            "run_id": run_id,
            "mode": mode,
            "strategy": {
                "name": strategy.name,
                "strategy_path": strategy.strategy_path,
                "config_path": strategy.config_path,
                "preset": preset_name,
                "parameters": resolved,
            },
            "markets": [market.__dict__ for market in markets],
            "venues": [
                {
                    "adapter": venue.adapter.name,
                    "adapter_options": public_adapter_options(venue.adapter_options),
                    "starting_balance": venue.starting_balance,
                    "base_currency": venue.base_currency,
                    "account_type": venue.account_type,
                    "oms_type": venue.oms_type,
                    "maker_fee_bps": venue.maker_fee_bps,
                    "taker_fee_bps": venue.taker_fee_bps,
                    "default_leverage": venue.default_leverage,
                    "leverages": venue.leverages,
                    "allow_cash_borrowing": venue.allow_cash_borrowing,
                    "liquidation_enabled": venue.liquidation_enabled,
                    "liquidation_trigger_ratio": venue.liquidation_trigger_ratio,
                    "liquidation_cancel_open_orders": venue.liquidation_cancel_open_orders,
                }
                for venue in venues
            ],
            "metrics": {},
        }
        runs_path = strategy_runs_path(strategy.name)
    if args.venue is not None:
        _select_trading_venue(record, args.venue)
    revision_path = strategies_root() / strategy.name / ".marketplace.json"
    if not revision_path.exists():
        source = python_inspect.getsourcefile(strategy.strategy_cls)
        if source is None:
            raise RuntimeError("strategy source is unavailable")
        revision_path = Path(source)
    revision = hashlib.sha256(revision_path.read_bytes()).digest()
    if args.dry_run:
        execute_trading(
            record=record,
            mode=mode,
            dry_run=True,
        )
        return 0
    with marketplace.strategy_operation_lock(strategy.name):
        authoritative = resolve_strategy(strategy.name)
        authoritative_revision_path = strategies_root() / authoritative.name / ".marketplace.json"
        if not authoritative_revision_path.exists():
            source = python_inspect.getsourcefile(authoritative.strategy_cls)
            if source is None:
                raise RuntimeError("strategy source is unavailable")
            authoritative_revision_path = Path(source)
        if hashlib.sha256(authoritative_revision_path.read_bytes()).digest() != revision:
            raise RuntimeError("strategy changed while the run was being prepared; retry the operation")
        if not args.run_id:
            run_dir = runs_path / run_id
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "run.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
            print(f"Run: {run_id}")
        register_running(runs_path, run_id)
    try:
        try:
            execute_trading(
                record=record,
                mode=mode,
                dry_run=False,
            )
        except BaseException as exc:
            try:
                record_execution_result(runs_path, run_id, failed=exc)
            except OSError:
                logging.getLogger("edgepilot.cli").exception(
                    "trading failure outcome could not be recorded",
                    extra={"event": "trading.execution.persist_failed", "run_id": run_id, "result": "failed"},
                )
            raise
        else:
            record_execution_result(runs_path, run_id)
    finally:
        unregister_running(runs_path, run_id)
    return 0


def _runs(args: argparse.Namespace) -> int:
    if args.action == "emergency-list":
        active = []
        for path in iter_run_directories():
            record = load_run(path.parent, path.name)
            if path.name in running_runs(path.parent):
                active.append({"run_id": path.name, "strategy": record.get("strategy", {}).get("name", ""), "mode": record.get("mode", "")})
        print(json.dumps({"runs": active}, indent=2))
        return 0
    if args.action == "list":
        print(f"{'RUN ID':<28} {'STATUS':<8} {'INSTRUMENT':<28} {'STRATEGY':<24} RETURN")
        for path in sorted(iter_run_directories(), reverse=True):
            record = load_run(path.parent, path.name)
            active = running_runs(path.parent)
            status = run_status(path.parent, path.name, record.get("mode"), active=active)
            instruments = ",".join(market["instrument_id"] for market in record.get("markets", []))
            strategy = record["strategy"]["name"]
            result = record.get("metrics", {}).get("return_pct")
            return_text = f"{result:+.2f}%" if result is not None else "-"
            print(f"{path.name:<28} {status:<8} {instruments:<48} {strategy:<24} {return_text}")
        return 0
    if args.action == "status":
        if args.run_id:
            directory = find_run_directory(args.run_id)
            record = load_run(directory.parent, args.run_id)
            print(run_status(directory.parent, args.run_id, record.get("mode")))
        else:
            active = {
                run_id: pid
                for path in iter_run_directories()
                for run_id, pid in running_runs(path.parent).items()
            }
            print(json.dumps(active, indent=2))
        return 0
    if args.action == "emergency-stop":
        if not args.run_id:
            raise ValueError("run ID is required for emergency-stop")
        directory = find_run_directory(args.run_id)
        if args.run_id not in running_runs(directory.parent):
            raise ValueError(f"Run is not active: {args.run_id}")
        request_emergency_stop(directory.parent, args.run_id)
        print(json.dumps({"run_id": args.run_id, "status": "EMERGENCY_STOPPING"}, indent=2))
        return 0
    if not args.run_id:
        raise ValueError("run ID is required for show")
    directory = find_run_directory(args.run_id)
    record = load_run(directory.parent, args.run_id)
    record["status"] = run_status(directory.parent, args.run_id, record.get("mode"))
    execution = load_execution(directory.parent, args.run_id)
    if execution:
        record["execution"] = execution
    print(json.dumps(record, indent=2))
    return 0


def _credentials(args: argparse.Namespace) -> int:
    if args.run_id:
        directory = find_run_directory(args.run_id)
        record = load_run(directory.parent, args.run_id)
        venues = [item["adapter"] for item in record["venues"]]
    else:
        if args.strategy:
            strategy = resolve_strategy(args.strategy)
            _, preset = load_preset(strategy, args.preset)
            venues = list(preset_venues(preset))
        else:
            venues = [args.venue]
    requirements = []
    for venue in venues:
        for item in credential_requirements(resolve_adapter(venue), args.mode):
            item = dict(item)
            item["adapter"] = venue.upper()
            requirements.append(item)
    print(
        json.dumps(
            {
                "adapters": [venue.upper() for venue in venues],
                "mode": args.mode,
                "configured": all(
                    item["configured"] for item in requirements if item["required"]
                ),
                "requirements": requirements,
            },
            indent=2,
        ),
    )
    return 0


def _marketplace(args: argparse.Namespace) -> int:
    if args.action == "history":
        print(json.dumps(marketplace.installation_history(), indent=2))
        return 0
    if args.action == "restore":
        print(json.dumps(marketplace.restore(strategy_slug=args.strategy or ""), indent=2))
        return 0
    if args.action == "clear":
        if not args.strategy:
            raise ValueError("--strategy is required to clear installation history")
        print(json.dumps(marketplace.clear_installation_history(args.strategy), indent=2))
        return 0
    if args.action == "search":
        print(json.dumps(marketplace.search(
            query=args.query,
            asset=args.asset,
            venue=args.venue,
            category=args.category,
            data_type=args.data_type,
            risk_profile=args.risk_profile or "",
            min_capacity_usd=args.min_capacity_usd,
            sort=args.sort,
            locale=args.locale,
        ), indent=2))
        return 0
    if not args.query or not args.version:
        raise ValueError("strategy slug and --version are required")
    if args.action == "inspect":
        print(json.dumps(marketplace.inspect(args.query, args.version, locale=args.locale), indent=2))
        return 0
    print(json.dumps(marketplace.download_and_install(args.query, args.version), indent=2))
    return 0


def _auth(args: argparse.Namespace) -> int:
    if args.all and args.local_only:
        raise ValueError("--all and --local-only cannot be combined")
    if args.action == "status":
        print(json.dumps(auth.status(), indent=2))
        return 0
    if args.action == "login":
        print(json.dumps(auth.login(), indent=2))
        return 0
    print(json.dumps(auth.logout(all_devices=bool(args.all), local_only=bool(args.local_only)), indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    clear_bound_account_key()
    logger = configure_logging()
    started = monotonic()
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        logger.warning(
            "command rejected by argument parser",
            extra={
                "event": "cli.command.rejected",
                "result": "rejected",
                "duration_ms": round((monotonic() - started) * 1000),
                "params": {"exit_code": exc.code},
            },
        )
        raise
    safe_params = {key: value for key, value in vars(args).items() if key not in {"adapter_set", "set"}}
    logger.info("command started", extra={"event": "cli.command.started", "params": safe_params})
    try:
        exempt = args.command in {"auth", "ui"} or (args.command == "runs" and args.action in {"emergency-list", "emergency-stop"})
        if args.command == "marketplace" and args.action == "install" and args.query and args.version:
            marketplace.preflight_install(args.query, args.version)
        if args.command == "marketplace" and args.action == "clear" and not args.strategy:
            raise ValueError("--strategy is required to clear installation history")
        if args.command == "strategies" and args.action == "remove":
            _preflight_strategy_removal(args.name, args.confirm)
        if args.command == "backtest":
            auth.authorize_backtest()
        elif not exempt and not auth.skip_auth_enabled():
            auth.access_token(interactive=True)
        if auth.skip_auth_enabled():
            load_env(STATE_ROOT / ".env")
        elif active_account_key() is not None:
            load_env(account_credentials_path())
        if args.command == "strategies":
            result = _strategies(args)
        elif args.command == "data":
            result = _data(args)
        elif args.command == "backtest":
            result = _backtest(args)
        elif args.command in {"paper", "demo", "live"}:
            result = _trade(args, args.command)
        elif args.command == "runs":
            result = _runs(args)
        elif args.command == "credentials":
            result = _credentials(args)
        elif args.command == "marketplace":
            result = _marketplace(args)
        elif args.command == "auth":
            result = _auth(args)
        else:
            if args.host != "127.0.0.1":
                raise ValueError("Dashboard must bind to 127.0.0.1")
            from edgepilot.local_service import ensure_service

            identity = ensure_service(port=args.port)
            print(f"EdgePilot dashboard: {identity['url']}")
            result = 0
        logger.info("command completed", extra={"event": "cli.command.completed", "result": "success", "duration_ms": round((monotonic() - started) * 1000), "params": safe_params})
        return result
    except marketplace.MarketplaceRequestError as exc:
        logger.exception("command failed", extra={"event": "cli.command.failed", "result": "failed", "duration_ms": round((monotonic() - started) * 1000), "params": safe_params})
        print(json.dumps({"error": {"code": exc.code, **exc.public_details()}}, separators=(",", ":")), file=sys.stderr)
        return 1
    except Exception as exc:
        logger.exception("command failed", extra={"event": "cli.command.failed", "result": "failed", "duration_ms": round((monotonic() - started) * 1000), "params": safe_params})
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
