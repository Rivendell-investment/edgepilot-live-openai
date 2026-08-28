from __future__ import annotations

import asyncio
from datetime import datetime
from datetime import timedelta
from datetime import timezone
import os
from decimal import Decimal
from pathlib import Path
from threading import Event
import time
import logging
from typing import Any
from typing import cast
from typing import get_type_hints

import pandas as pd

from nautilus_trader.backtest.node import BacktestNode
from nautilus_trader.common.config import resolve_path
from nautilus_trader.model.data import Bar
from nautilus_trader.model.data import BarType
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.identifiers import ClientId
from nautilus_trader.model.instruments import instruments_from_pyo3
from nautilus_trader.live.factories import LiveDataClientFactory
from nautilus_trader.persistence.catalog import ParquetDataCatalog
from nautilus_trader.persistence.catalog.parquet import _parse_filename_timestamps
from nautilus_trader.persistence.config import DataCatalogConfig
from tqdm import tqdm

from edgepilot.strategies.discovery import AdapterDescriptor
from edgepilot.strategies.discovery import instantiate_config
from edgepilot.execution.environment import apply_proxy_url


UTC = timezone.utc
LOGGER = logging.getLogger("edgepilot.backtesting.catalog")


def parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _https_proxy_url() -> str | None:
    """Forward a standard process-local HTTPS proxy to native HTTP clients."""
    return os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or None


def _binance_request_window(
    start: datetime,
    end: datetime,
    interval_ns: int,
) -> tuple[int, int, int, int]:
    """Map an inclusive close-time gap to Binance's open-time request window."""
    if interval_ns <= 0:
        raise ValueError("Binance bar interval must be positive")
    start_ns = int(start.timestamp() * 1_000_000_000)
    end_ns = int(end.timestamp() * 1_000_000_000)
    first_close_ns = ((start_ns + interval_ns - 1) // interval_ns) * interval_ns
    last_close_ns = (end_ns // interval_ns) * interval_ns
    if first_close_ns > last_close_ns:
        raise ValueError("Binance request period contains no complete bar close")
    return (
        (first_close_ns - interval_ns) // 1_000_000,
        (last_close_ns - interval_ns) // 1_000_000,
        first_close_ns,
        last_close_ns,
    )


def _client_config(
    adapter: AdapterDescriptor,
    options: dict[str, Any],
    instrument_id: str,
):
    config_cls = resolve_path(adapter.data_config_path)
    values = dict(options)
    if "instrument_provider" in get_type_hints(config_cls):
        values.setdefault(
            "instrument_provider",
            {"load_all": False, "load_ids": [instrument_id]},
        )
    # Catalog pulls reach the venue from the same machine as a trading run, so
    # they follow the account proxy rather than requiring their own setting.
    apply_proxy_url(values, adapter.data_config_path)
    return instantiate_config(adapter.data_config_path, values)


def pull_data(
    *,
    catalog_path: Path,
    adapter: AdapterDescriptor,
    instrument_id: str,
    data_type: str,
    start: datetime,
    end: datetime,
    bar_type: str | None,
    adapter_options: dict[str, Any],
) -> None:
    """Request native historical data and persist it through Nautilus's catalog writer."""
    started = time.monotonic()
    log_params = {"venue": adapter.name, "instrument": instrument_id, "data_type": data_type, "bar_type": bar_type, "start": start.isoformat(), "end": end.isoformat(), "adapter_options": adapter_options}
    LOGGER.info("data pull started", extra={"event": "data.pull.started", "params": log_params})
    catalog_path.mkdir(parents=True, exist_ok=True)
    normalized = data_type.replace("_", "-").lower()
    if adapter.name == "ALPACA" and normalized == "bars":
        if not bar_type:
            raise ValueError("--bar-type is required when --data-type=bars")
        asyncio.run(
            _pull_alpaca_bars(
                catalog_path=catalog_path,
                adapter=adapter,
                instrument_id=instrument_id,
                bar_type=bar_type,
                start=start,
                end=end,
                adapter_options=adapter_options,
            ),
        )
        LOGGER.info("data pull completed", extra={"event": "data.pull.completed", "result": "success", "duration_ms": round((time.monotonic() - started) * 1000), "params": log_params})
        return
    if adapter.name == "OKX" and normalized == "bars":
        if not bar_type:
            raise ValueError("--bar-type is required when --data-type=bars")
        asyncio.run(
            _pull_okx_bars(
                catalog_path=catalog_path,
                adapter=adapter,
                instrument_id=instrument_id,
                bar_type=bar_type,
                start=start,
                end=end,
                adapter_options=adapter_options,
            ),
        )
        LOGGER.info("data pull completed", extra={"event": "data.pull.completed", "result": "success", "duration_ms": round((time.monotonic() - started) * 1000), "params": log_params})
        return
    if adapter.name == "BINANCE" and normalized == "bars":
        if not bar_type:
            raise ValueError("--bar-type is required when --data-type=bars")
        asyncio.run(
            _pull_binance_bars(
                catalog_path=catalog_path,
                adapter=adapter,
                instrument_id=instrument_id,
                bar_type=bar_type,
                start=start,
                end=end,
                adapter_options=adapter_options,
            ),
        )
        LOGGER.info("data pull completed", extra={"event": "data.pull.completed", "result": "success", "duration_ms": round((time.monotonic() - started) * 1000), "params": log_params})
        return
    if adapter.name == "BITGET" and normalized == "bars":
        if not bar_type:
            raise ValueError("--bar-type is required when --data-type=bars")
        asyncio.run(
            _pull_bitget_bars(
                catalog_path=catalog_path,
                adapter=adapter,
                instrument_id=instrument_id,
                bar_type=bar_type,
                start=start,
                end=end,
                adapter_options=adapter_options,
            ),
        )
        LOGGER.info("data pull completed", extra={"event": "data.pull.completed", "result": "success", "duration_ms": round((time.monotonic() - started) * 1000), "params": log_params})
        return
    if adapter.name == "GATEIO" and normalized == "bars":
        if not bar_type:
            raise ValueError("--bar-type is required when --data-type=bars")
        asyncio.run(
            _pull_gateio_bars(
                catalog_path=catalog_path,
                adapter=adapter,
                instrument_id=instrument_id,
                bar_type=bar_type,
                start=start,
                end=end,
                adapter_options=adapter_options,
            ),
        )
        LOGGER.info("data pull completed", extra={"event": "data.pull.completed", "result": "success", "duration_ms": round((time.monotonic() - started) * 1000), "params": log_params})
        return
    if adapter.name == "DIGIFINEX" and normalized == "bars":
        if not bar_type:
            raise ValueError("--bar-type is required when --data-type=bars")
        asyncio.run(
            _pull_digifinex_bars(
                catalog_path=catalog_path,
                adapter=adapter,
                instrument_id=instrument_id,
                bar_type=bar_type,
                start=start,
                end=end,
                adapter_options=adapter_options,
            ),
        )
        LOGGER.info("data pull completed", extra={"event": "data.pull.completed", "result": "success", "duration_ms": round((time.monotonic() - started) * 1000), "params": log_params})
        return

    client_name = adapter.name.lower()
    node = BacktestNode([])
    factory_cls = cast(type[LiveDataClientFactory], resolve_path(adapter.data_factory_path))
    node.add_data_client_factory(client_name, factory_cls)
    node.setup_download_engine(
        DataCatalogConfig(path=str(catalog_path)),
        {client_name: _client_config(adapter, adapter_options, instrument_id)},
    )

    instrument = InstrumentId.from_str(instrument_id)
    _download_and_wait(
        node,
        "request_instrument",
        instrument_id=instrument,
        client_id=ClientId(client_name),
    )
    # The download actor callback can precede the adapter's cache update by one
    # event-loop turn. Let the native client finish that update before bars.
    time.sleep(0.25)
    catalog = ParquetDataCatalog(str(catalog_path))
    if not catalog.instruments(instrument_ids=[instrument_id]):
        node.dispose()
        raise RuntimeError(f"Native {adapter.name} adapter did not return {instrument_id}")

    request_name: str
    request_args: dict[str, Any]
    if normalized == "bars":
        if not bar_type:
            raise ValueError("--bar-type is required when --data-type=bars")
        request_name = "request_bars"
        request_args = {"bar_type": BarType.from_str(bar_type)}
    elif normalized == "trades":
        request_name = "request_trade_ticks"
        request_args = {"instrument_id": instrument}
    elif normalized == "quotes":
        request_name = "request_quote_ticks"
        request_args = {"instrument_id": instrument}
    elif normalized == "order-book-depth":
        request_name = "request_order_book_depth"
        request_args = {"instrument_id": instrument}
    elif normalized == "order-book-deltas":
        request_name = "request_order_book_deltas"
        request_args = {"instrument_id": instrument}
    else:
        raise ValueError(f"Unsupported native data type: {data_type}")

    _download_and_wait(
        node,
        request_name,
        start=pd.Timestamp(start),
        end=pd.Timestamp(end),
        client_id=ClientId(client_name),
        **request_args,
    )
    node.dispose()

    if normalized == "bars":
        assert bar_type is not None
        _normalize_downloaded_bars(catalog_path, bar_type, start, end)
        start_ns = int(start.timestamp() * 1_000_000_000)
        end_ns = int(end.timestamp() * 1_000_000_000)
        downloaded = [
            bar
            for bar in ParquetDataCatalog(str(catalog_path)).bars(bar_types=[bar_type])
            if start_ns <= bar.ts_event <= end_ns
        ]
        if not downloaded:
            raise RuntimeError(
                f"Native {adapter.name} adapter returned no {bar_type} bars for the requested period",
            )
    LOGGER.info("data pull completed", extra={"event": "data.pull.completed", "result": "success", "duration_ms": round((time.monotonic() - started) * 1000), "params": log_params})


async def _pull_alpaca_bars(
    *,
    catalog_path: Path,
    adapter: AdapterDescriptor,
    instrument_id: str,
    bar_type: str,
    start: datetime,
    end: datetime,
    adapter_options: dict[str, Any],
) -> None:
    """Download Alpaca bars via the Python HTTP client and write them to the catalog."""
    from nautilus_trader.adapters.alpaca.data import alpaca_interval_from_bar_type
    from nautilus_trader.adapters.alpaca.data import bars_from_alpaca
    from nautilus_trader.adapters.alpaca.factories import get_cached_alpaca_http_client
    from nautilus_trader.adapters.alpaca.providers import instrument_from_asset
    from nautilus_trader.adapters.alpaca.symbol import is_crypto_symbol
    from nautilus_trader.adapters.alpaca.symbol import parse_alpaca_instrument_id
    from urllib.parse import quote

    config = _client_config(adapter, adapter_options, instrument_id)
    demo = False
    if hasattr(config, "resolved_environment"):
        demo = bool(config.resolved_environment().is_demo)
    client = get_cached_alpaca_http_client(
        api_key=config.api_key,
        api_secret=config.api_secret,
        base_url=config.base_url_http,
        data_url=getattr(config, "base_url_data", None),
        demo=demo,
    )

    native, _listing = parse_alpaca_instrument_id(InstrumentId.from_str(instrument_id))
    asset = await client.request("get", f"/v2/assets/{quote(native, safe='')}")
    instrument = instrument_from_asset(asset, 0)
    if str(instrument.id) != instrument_id:
        raise RuntimeError(f"Alpaca asset {native} resolved to {instrument.id}, not {instrument_id}")

    catalog = ParquetDataCatalog(str(catalog_path))
    catalog.write_data([instrument])

    parsed_bar_type = BarType.from_str(bar_type)
    alpaca_interval_from_bar_type(parsed_bar_type)
    bar_delta = parsed_bar_type.spec.timedelta
    intervals = missing_bar_intervals(catalog_path, bar_type, start, end)
    existing_events = {bar.ts_event for bar in catalog.bars(bar_types=[bar_type])}
    expected_total = sum(
        max(0, int((interval_end - interval_start) / bar_delta) + 1)
        for interval_start, interval_end in intervals
    )
    crypto = is_crypto_symbol(native)
    path = "/v1beta3/crypto/us/bars" if crypto else "/v2/stocks/bars"
    with tqdm(total=expected_total, unit="bar", desc=f"ALPACA {bar_delta} bars") as progress:
        for interval_start, interval_end in intervals:
            params: dict[str, Any] = {
                "symbols": native,
                "timeframe": alpaca_interval_from_bar_type(parsed_bar_type),
                "start": interval_start.isoformat(),
                "end": interval_end.isoformat(),
                "limit": 10000,
                "sort": "asc",
            }
            if not crypto:
                params["adjustment"] = getattr(config, "adjustment", "all")
                params["feed"] = getattr(config, "feed", "iex")
            page_token = None
            interval_bars: list[Bar] = []
            while True:
                if page_token:
                    params["page_token"] = page_token
                payload = await client.request("get", path, params, data=True)
                rows = (payload or {}).get("bars", {}).get(native) or []
                interval_bars.extend(
                    bars_from_alpaca(
                        parsed_bar_type,
                        instrument,
                        rows,
                        use_regular_trading_hours=getattr(config, "use_regular_trading_hours", True),
                    ),
                )
                page_token = (payload or {}).get("next_page_token")
                if not page_token:
                    break
            start_ns = int(interval_start.timestamp() * 1_000_000_000)
            end_ns = int(interval_end.timestamp() * 1_000_000_000)
            interval_bars = [
                bar
                for bar in interval_bars
                if start_ns <= bar.ts_event <= end_ns and bar.ts_event not in existing_events
            ]
            progress.update(min(len(interval_bars), max(0, expected_total - progress.n)))
            normalized_bars = []
            for bar in interval_bars:
                values = Bar.to_dict(bar)
                values["ts_init"] = values["ts_event"]
                normalized_bars.append(Bar.from_dict(values))
            if normalized_bars:
                catalog.write_data(normalized_bars)
                existing_events.update(bar.ts_event for bar in normalized_bars)

    start_ns = int(start.timestamp() * 1_000_000_000)
    end_ns = int(end.timestamp() * 1_000_000_000)
    downloaded = [
        bar
        for bar in ParquetDataCatalog(str(catalog_path)).bars(bar_types=[bar_type])
        if start_ns <= bar.ts_event <= end_ns
    ]
    if not downloaded:
        raise RuntimeError(
            f"Native Alpaca adapter returned no {bar_type} bars for the requested period",
        )


async def _pull_okx_bars(
    *,
    catalog_path: Path,
    adapter: AdapterDescriptor,
    instrument_id: str,
    bar_type: str,
    start: datetime,
    end: datetime,
    adapter_options: dict[str, Any],
) -> None:
    """Follow Nautilus's official OKX historical-bars example."""
    from nautilus_trader.adapters.okx.factories import get_cached_okx_http_client
    from nautilus_trader.core import nautilus_pyo3

    config = _client_config(adapter, adapter_options, instrument_id)
    environment = config.environment or nautilus_pyo3.OKXEnvironment.LIVE
    # Nautilus's OKX client constructor resolves absent credentials from the
    # environment even for public market-data calls. Non-secret placeholders
    # prevent that lookup; these endpoints do not authenticate the request.
    public_only = not (config.api_key or config.api_secret or config.api_passphrase)
    client = get_cached_okx_http_client(
        api_key="PUBLIC_DATA_ONLY" if public_only else config.api_key,
        api_secret="PUBLIC_DATA_ONLY" if public_only else config.api_secret,
        api_passphrase="PUBLIC_DATA_ONLY" if public_only else config.api_passphrase,
        base_url=config.base_url_http,
        timeout_secs=config.http_timeout_secs,
        max_retries=config.max_retries,
        retry_delay_ms=config.retry_delay_initial_ms,
        retry_delay_max_ms=config.retry_delay_max_ms,
        environment=environment,
        proxy_url=config.proxy_url,
    )

    native_instruments = []
    for instrument_type in config.instrument_types:
        families = config.instrument_families or (None,)
        for family in families:
            instruments, _ = await client.request_instruments(instrument_type, family)
            native_instruments.extend(instruments)
            for instrument in instruments:
                client.cache_instrument(instrument)

    target = next(
        (item for item in native_instruments if str(item.id) == instrument_id),
        None,
    )
    if target is None:
        raise RuntimeError(f"Native OKX client did not return {instrument_id}")

    catalog = ParquetDataCatalog(str(catalog_path))
    catalog.write_data([instruments_from_pyo3([target])[0]])
    native_bar_type = nautilus_pyo3.BarType.from_str(bar_type)
    interval = BarType.from_str(bar_type).spec.timedelta
    intervals = missing_bar_intervals(catalog_path, bar_type, start, end)
    existing_events = {
        bar.ts_event for bar in catalog.bars(bar_types=[bar_type])
    }
    expected_total = sum(
        max(0, int((interval_end - interval_start) / interval) + 1)
        for interval_start, interval_end in intervals
    )
    with tqdm(total=expected_total, unit="bar", desc=f"OKX {interval} bars") as progress:
        for interval_start, interval_end in intervals:
            # OKX's historical candles response excludes the exact request
            # endpoints. Pad by one bar, then filter to the catalog interval.
            start_cursor = interval_start - interval
            end_cursor = interval_end + interval
            pages: list[list[Bar]] = []
            while end_cursor > start_cursor:
                native_page = await client.request_bars(
                    bar_type=native_bar_type,
                    start=start_cursor,
                    end=end_cursor,
                    limit=100,
                )
                if not native_page:
                    break
                page = Bar.from_pyo3_list(native_page)
                pages.append(page)
                first_event = datetime.fromtimestamp(page[0].ts_event / 1_000_000_000, UTC)
                progress.update(min(len(page), max(0, expected_total - progress.n)))
                progress.set_postfix(from_time=first_event.isoformat())
                next_end = first_event - timedelta(milliseconds=1)
                if next_end >= end_cursor:
                    raise RuntimeError("Native OKX pagination cursor did not advance")
                end_cursor = next_end
                if first_event <= start_cursor:
                    break

            interval_bars = [bar for page in reversed(pages) for bar in page]
            start_ns = int(interval_start.timestamp() * 1_000_000_000)
            end_ns = int(interval_end.timestamp() * 1_000_000_000)
            interval_bars = [
                bar
                for bar in interval_bars
                if start_ns <= bar.ts_event <= end_ns and bar.ts_event not in existing_events
            ]

            normalized_bars = []
            for bar in interval_bars:
                values = Bar.to_dict(bar)
                values["ts_init"] = values["ts_event"]
                normalized_bars.append(Bar.from_dict(values))
            if normalized_bars:
                catalog.write_data(normalized_bars)
                existing_events.update(bar.ts_event for bar in normalized_bars)


async def _pull_bitget_bars(
    *,
    catalog_path: Path,
    adapter: AdapterDescriptor,
    instrument_id: str,
    bar_type: str,
    start: datetime,
    end: datetime,
    adapter_options: dict[str, Any],
) -> None:
    """Download Bitget UTA bars via the PyO3 HTTP client and write them to the catalog."""
    from nautilus_trader.adapters.bitget.data import bitget_interval_from_bar_type
    from nautilus_trader.adapters.bitget.factories import get_cached_bitget_http_client
    from nautilus_trader.adapters.bitget.symbol import parse_bitget_instrument_id
    from nautilus_trader.model.instruments import instruments_from_pyo3

    config = _client_config(adapter, adapter_options, instrument_id)
    demo = False
    if hasattr(config, "resolved_environment"):
        demo = bool(config.resolved_environment().is_demo)
    client = get_cached_bitget_http_client(
        api_key=config.api_key,
        api_secret=config.api_secret,
        api_passphrase=config.api_passphrase,
        base_url=config.base_url_http,
        demo=demo,
    )

    raw_symbol, product_type = parse_bitget_instrument_id(InstrumentId.from_str(instrument_id))
    native_instruments = await client.request_instruments(product_type.value)
    target = next(
        (item for item in native_instruments if str(item.id) == instrument_id),
        None,
    )
    if target is None:
        raise RuntimeError(f"Native Bitget client did not return {instrument_id}")

    catalog = ParquetDataCatalog(str(catalog_path))
    catalog.write_data([instruments_from_pyo3([target])[0]])

    parsed_bar_type = BarType.from_str(bar_type)
    interval = bitget_interval_from_bar_type(parsed_bar_type)
    bar_delta = parsed_bar_type.spec.timedelta
    intervals = missing_bar_intervals(catalog_path, bar_type, start, end)
    existing_events = {bar.ts_event for bar in catalog.bars(bar_types=[bar_type])}
    expected_total = sum(
        max(0, int((interval_end - interval_start) / bar_delta) + 1)
        for interval_start, interval_end in intervals
    )
    with tqdm(total=expected_total, unit="bar", desc=f"BITGET {bar_delta} bars") as progress:
        for interval_start, interval_end in intervals:
            start_ms = int(interval_start.timestamp() * 1000)
            end_ms = int(interval_end.timestamp() * 1000)
            native_page = await client.request_bars(
                bar_type=bar_type,
                product_type=product_type.value,
                symbol=raw_symbol,
                interval=interval,
                limit=1000,
                start_ms=start_ms,
                end_ms=end_ms,
            )
            if not native_page:
                continue
            page = Bar.from_pyo3_list(native_page)
            start_ns = int(interval_start.timestamp() * 1_000_000_000)
            end_ns = int(interval_end.timestamp() * 1_000_000_000)
            interval_bars = [
                bar
                for bar in page
                if start_ns <= bar.ts_event <= end_ns and bar.ts_event not in existing_events
            ]
            progress.update(min(len(interval_bars), max(0, expected_total - progress.n)))
            normalized_bars = []
            for bar in interval_bars:
                values = Bar.to_dict(bar)
                values["ts_init"] = values["ts_event"]
                normalized_bars.append(Bar.from_dict(values))
            if normalized_bars:
                catalog.write_data(normalized_bars)
                existing_events.update(bar.ts_event for bar in normalized_bars)

    start_ns = int(start.timestamp() * 1_000_000_000)
    end_ns = int(end.timestamp() * 1_000_000_000)
    downloaded = [
        bar
        for bar in ParquetDataCatalog(str(catalog_path)).bars(bar_types=[bar_type])
        if start_ns <= bar.ts_event <= end_ns
    ]
    if not downloaded:
        raise RuntimeError(
            f"Native Bitget adapter returned no {bar_type} bars for the requested period",
        )


async def _pull_gateio_bars(
    *,
    catalog_path: Path,
    adapter: AdapterDescriptor,
    instrument_id: str,
    bar_type: str,
    start: datetime,
    end: datetime,
    adapter_options: dict[str, Any],
) -> None:
    """Download Gate.io USDT perpetual bars via the PyO3 HTTP client and write them to the catalog."""
    from nautilus_trader.adapters.gateio.data import gateio_interval_from_bar_type
    from nautilus_trader.adapters.gateio.factories import get_cached_gateio_http_client
    from nautilus_trader.adapters.gateio.symbol import parse_gateio_instrument_id
    from nautilus_trader.model.instruments import instruments_from_pyo3

    config = _client_config(adapter, adapter_options, instrument_id)
    demo = False
    if hasattr(config, "resolved_environment"):
        demo = bool(config.resolved_environment().is_demo)
    client = get_cached_gateio_http_client(
        api_key=config.api_key,
        api_secret=config.api_secret,
        base_url=config.base_url_http,
        demo=demo,
    )

    raw_symbol, product_type = parse_gateio_instrument_id(InstrumentId.from_str(instrument_id))
    settle = product_type.settle
    if settle is None:
        raise RuntimeError(f"Gate.io product {product_type.value} has no settle currency")

    native_instruments = await client.request_instruments(settle)
    target = next(
        (item for item in native_instruments if str(item.id) == instrument_id),
        None,
    )
    if target is None:
        raise RuntimeError(f"Native Gate.io client did not return {instrument_id}")

    catalog = ParquetDataCatalog(str(catalog_path))
    catalog.write_data([instruments_from_pyo3([target])[0]])

    parsed_bar_type = BarType.from_str(bar_type)
    interval = gateio_interval_from_bar_type(parsed_bar_type)
    bar_delta = parsed_bar_type.spec.timedelta
    intervals = missing_bar_intervals(catalog_path, bar_type, start, end)
    existing_events = {bar.ts_event for bar in catalog.bars(bar_types=[bar_type])}
    expected_total = sum(
        max(0, int((interval_end - interval_start) / bar_delta) + 1)
        for interval_start, interval_end in intervals
    )
    with tqdm(total=expected_total, unit="bar", desc=f"GATEIO {bar_delta} bars") as progress:
        for interval_start, interval_end in intervals:
            start_ms = int(interval_start.timestamp() * 1000)
            end_ms = int(interval_end.timestamp() * 1000)
            native_page = await client.request_bars(
                bar_type=bar_type,
                settle=settle,
                contract=raw_symbol,
                interval=interval,
                limit=1000,
                start_ms=start_ms,
                end_ms=end_ms,
            )
            if not native_page:
                continue
            page = Bar.from_pyo3_list(native_page)
            start_ns = int(interval_start.timestamp() * 1_000_000_000)
            end_ns = int(interval_end.timestamp() * 1_000_000_000)
            interval_bars = [
                bar
                for bar in page
                if start_ns <= bar.ts_event <= end_ns and bar.ts_event not in existing_events
            ]
            progress.update(min(len(interval_bars), max(0, expected_total - progress.n)))
            normalized_bars = []
            for bar in interval_bars:
                values = Bar.to_dict(bar)
                values["ts_init"] = values["ts_event"]
                normalized_bars.append(Bar.from_dict(values))
            if normalized_bars:
                catalog.write_data(normalized_bars)
                existing_events.update(bar.ts_event for bar in normalized_bars)

    start_ns = int(start.timestamp() * 1_000_000_000)
    end_ns = int(end.timestamp() * 1_000_000_000)
    downloaded = [
        bar
        for bar in ParquetDataCatalog(str(catalog_path)).bars(bar_types=[bar_type])
        if start_ns <= bar.ts_event <= end_ns
    ]
    if not downloaded:
        raise RuntimeError(
            f"Native Gate.io adapter returned no {bar_type} bars for the requested period",
        )


async def _pull_digifinex_bars(
    *,
    catalog_path: Path,
    adapter: AdapterDescriptor,
    instrument_id: str,
    bar_type: str,
    start: datetime,
    end: datetime,
    adapter_options: dict[str, Any],
) -> None:
    """Download DigiFinex Swap perpetual bars via the REST client and write them to the catalog."""
    from urllib.request import Request
    from urllib.request import urlopen

    from nautilus_trader.adapters.digifinex.data import request_digifinex_bars_paginated
    from nautilus_trader.adapters.digifinex.http import DigifinexHttpClient
    from nautilus_trader.adapters.digifinex.parsing import digifinex_granularity_from_bar_type
    from nautilus_trader.adapters.digifinex.providers import instrument_from_digifinex
    from nautilus_trader.adapters.digifinex.symbol import parse_digifinex_instrument_id

    def _opener(request: Request, timeout: float | None = None) -> Any:
        # Cloudflare in front of openapi.digifinex.com rejects urllib's default
        # Python User-Agent with error 1010; identify this downloader instead.
        request.add_header("User-Agent", "edgepilot-catalog")
        return urlopen(request, timeout=timeout)  # noqa: S310 - client validates http(s)

    config = _client_config(adapter, adapter_options, instrument_id)
    client = DigifinexHttpClient(
        api_key=config.api_key,
        api_secret=config.api_secret,
        base_url=config.resolved_base_url_http(),
        opener=_opener,
    )

    raw_symbol = parse_digifinex_instrument_id(InstrumentId.from_str(instrument_id))
    payload = await client.request(
        "GET",
        "/public/instrument",
        params={"instrument_id": raw_symbol},
    )
    instrument = instrument_from_digifinex(payload, time.time_ns())
    if str(instrument.id) != instrument_id:
        raise RuntimeError(
            f"Native DigiFinex client resolved {raw_symbol} to {instrument.id}, not {instrument_id}",
        )

    catalog = ParquetDataCatalog(str(catalog_path))
    catalog.write_data([instrument])

    parsed_bar_type = BarType.from_str(bar_type)
    digifinex_granularity_from_bar_type(parsed_bar_type)
    bar_delta = parsed_bar_type.spec.timedelta
    intervals = missing_bar_intervals(catalog_path, bar_type, start, end)
    existing_events = {bar.ts_event for bar in catalog.bars(bar_types=[bar_type])}
    expected_total = sum(
        max(0, int((interval_end - interval_start) / bar_delta) + 1)
        for interval_start, interval_end in intervals
    )
    with tqdm(total=expected_total, unit="bar", desc=f"DIGIFINEX {bar_delta} bars") as progress:
        for interval_start, interval_end in intervals:
            # The adapter advances an explicit start/end window in ascending
            # pages capped by the exchange page limit (currently 100 candles;
            # a raised exchange limit benefits this loop without changes here).
            # ``limit=0`` disables the client-side total-bar cap.
            page = await request_digifinex_bars_paginated(
                client,
                bar_type=parsed_bar_type,
                start=pd.Timestamp(interval_start),
                end=pd.Timestamp(interval_end),
                limit=0,
                ts_init=time.time_ns(),
            )
            if not page:
                continue
            start_ns = int(interval_start.timestamp() * 1_000_000_000)
            end_ns = int(interval_end.timestamp() * 1_000_000_000)
            interval_bars = [
                bar
                for bar in page
                if start_ns <= bar.ts_event <= end_ns and bar.ts_event not in existing_events
            ]
            progress.update(min(len(interval_bars), max(0, expected_total - progress.n)))
            normalized_bars = []
            for bar in interval_bars:
                values = Bar.to_dict(bar)
                values["ts_init"] = values["ts_event"]
                # DigiFinex candle strings drop trailing zeros, so a candle can
                # parse below the instrument's price precision. The backtest
                # matching engine rejects bars whose precision differs from the
                # instrument, so re-quantize every field before persisting.
                for field in ("open", "high", "low", "close"):
                    values[field] = f"{Decimal(values[field]):.{instrument.price_precision}f}"
                values["volume"] = f"{Decimal(values['volume']):.{instrument.size_precision}f}"
                normalized_bars.append(Bar.from_dict(values))
            if normalized_bars:
                catalog.write_data(normalized_bars)
                existing_events.update(bar.ts_event for bar in normalized_bars)

    start_ns = int(start.timestamp() * 1_000_000_000)
    end_ns = int(end.timestamp() * 1_000_000_000)
    downloaded = [
        bar
        for bar in ParquetDataCatalog(str(catalog_path)).bars(bar_types=[bar_type])
        if start_ns <= bar.ts_event <= end_ns
    ]
    if not downloaded:
        raise RuntimeError(
            f"Native DigiFinex adapter returned no {bar_type} bars for the requested period",
        )


async def _pull_binance_bars(
    *,
    catalog_path: Path,
    adapter: AdapterDescriptor,
    instrument_id: str,
    bar_type: str,
    start: datetime,
    end: datetime,
    adapter_options: dict[str, Any],
) -> None:
    """Download Binance Futures bars using its native provider and HTTP market API.

    Binance's backtest download client only serves an instrument after its live
    provider has initialized.  The download-node path does not connect that
    provider first, so use the adapter's own provider and market API directly.
    """
    from nautilus_trader.adapters.binance.common.enums import BinanceEnvironment
    from nautilus_trader.adapters.binance.common.enums import BinanceKlineInterval
    from nautilus_trader.adapters.binance.factories import get_cached_binance_http_client
    from nautilus_trader.adapters.binance.futures.enums import BinanceFuturesEnumParser
    from nautilus_trader.adapters.binance.futures.http.market import BinanceFuturesMarketHttpAPI
    from nautilus_trader.adapters.binance.futures.providers import BinanceFuturesInstrumentProvider
    from nautilus_trader.common.component import LiveClock

    config = _client_config(adapter, adapter_options, instrument_id)
    if not config.account_type.is_futures:
        raise ValueError("Binance bar download currently requires a Futures account type")
    clock = LiveClock()
    environment = config.environment or BinanceEnvironment.LIVE
    client = get_cached_binance_http_client(
        clock=clock,
        account_type=config.account_type,
        api_key=config.api_key,
        api_secret=config.api_secret,
        key_type=config.key_type,
        base_url=config.base_url_http,
        environment=environment,
        is_us=config.us,
        proxy_url=config.proxy_url or _https_proxy_url(),
    )
    catalog = ParquetDataCatalog(str(catalog_path))
    cached_instruments = catalog.instruments(instrument_ids=[instrument_id])
    target = cached_instruments[0] if cached_instruments else None
    if target is None:
        provider = BinanceFuturesInstrumentProvider(
            client=client,
            clock=clock,
            account_type=config.account_type,
            config=config.instrument_provider,
        )
        await provider.initialize()
        target = provider.find(InstrumentId.from_str(instrument_id))
    if target is None:
        raise RuntimeError(f"Native Binance adapter did not return {instrument_id}")

    native_bar_type = BarType.from_str(bar_type)
    interval_ns = int(native_bar_type.spec.timedelta.total_seconds() * 1_000_000_000)
    request_start_ms, request_end_ms, first_close_ns, last_close_ns = _binance_request_window(
        start,
        end,
        interval_ns,
    )
    _normalize_binance_catalog_bars(catalog, bar_type, interval_ns)
    resolution = BinanceFuturesEnumParser().parse_nautilus_bar_aggregation(
        native_bar_type.spec.aggregation,
    )
    try:
        interval = BinanceKlineInterval(f"{native_bar_type.spec.step}{resolution}")
    except ValueError as exc:
        raise ValueError(f"Unsupported Binance bar interval: {native_bar_type.spec}") from exc
    market = BinanceFuturesMarketHttpAPI(client, account_type=config.account_type)
    downloaded = await market.request_binance_bars(
        bar_type=native_bar_type,
        interval=interval,
        start_time=request_start_ms,
        end_time=request_end_ms,
        limit=1_000,
    )
    now_ns = clock.timestamp_ns()
    existing_events = {bar.ts_event for bar in catalog.bars(bar_types=[bar_type])}
    normalized_bars = []
    for bar in downloaded:
        if bar.ts_event >= now_ns:
            continue
        values = Bar.to_dict(bar)
        # Binance reports a completed kline at close - 1 ms.  Nautilus's
        # catalog interval checker expects close-stamped external bars.
        close_ns = ((bar.ts_event + interval_ns - 1) // interval_ns) * interval_ns
        if close_ns < first_close_ns or close_ns > last_close_ns or close_ns in existing_events:
            continue
        values["ts_event"] = close_ns
        values["ts_init"] = close_ns
        normalized_bars.append(Bar.from_dict(values))
    if not normalized_bars and not existing_events:
        raise RuntimeError(f"Native Binance adapter returned no complete {bar_type} bars")
    catalog.write_data([target])
    if normalized_bars:
        _write_contiguous_bars(catalog, normalized_bars, interval_ns)


def _write_contiguous_bars(
    catalog: ParquetDataCatalog,
    bars: list[Bar],
    interval_ns: int,
) -> None:
    """Persist each contiguous Binance range separately.

    A catalog parquet file represents one continuous time interval. A later
    download can legitimately span an already-cached middle range, so writing
    its remaining bars as one file would create an overlapping interval even
    though no bar timestamp is duplicated.
    """
    ordered = sorted(bars, key=lambda bar: bar.ts_event)
    segment = [ordered[0]]
    for bar in ordered[1:]:
        if bar.ts_event == segment[-1].ts_event + interval_ns:
            segment.append(bar)
            continue
        catalog.write_data(segment)
        segment = [bar]
    catalog.write_data(segment)


def _normalize_binance_catalog_bars(
    catalog: ParquetDataCatalog,
    bar_type: str,
    interval_ns: int,
) -> None:
    """Migrate Binance's close-minus-one-millisecond klines once in place."""
    files = catalog.filter_files(
        Bar,
        catalog.get_file_list_from_data_cls(Bar),
        identifiers=[bar_type],
    )
    for file_path in files:
        bars = catalog.query(Bar, files=[file_path])
        if not bars or all(bar.ts_event % interval_ns == 0 for bar in bars):
            continue
        normalized = []
        for bar in bars:
            values = Bar.to_dict(bar)
            close_ns = ((bar.ts_event + interval_ns - 1) // interval_ns) * interval_ns
            values["ts_event"] = close_ns
            values["ts_init"] = close_ns
            normalized.append(Bar.from_dict(values))
        catalog.fs.rm(file_path)
        catalog.write_data(normalized)


def _download_and_wait(node: BacktestNode, request_name: str, **kwargs: Any) -> None:
    completed = Event()

    def on_complete(_: Any) -> None:
        completed.set()

    node.download_data(request_name, callback=on_complete, **kwargs)
    deadline = time.monotonic() + 1_800
    while not completed.wait(0.25):
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Timed out waiting for Nautilus {request_name}")


def _normalize_downloaded_bars(
    catalog_path: Path,
    bar_type: str,
    start: datetime,
    end: datetime,
) -> None:
    """Make venue-downloaded close-stamped bars replayable by event time.

    Live adapters initialize historical responses when they are downloaded. A
    backtest must instead replay a completed external bar at its close timestamp.
    Nautilus documents ``ts_init == ts_event`` for close-stamped external bars.
    """
    catalog = ParquetDataCatalog(str(catalog_path))
    files = catalog.filter_files(
        Bar,
        catalog.get_file_list_from_data_cls(Bar),
        identifiers=[bar_type],
        start=int(start.timestamp() * 1_000_000_000),
        end=int(end.timestamp() * 1_000_000_000),
    )
    for file_path in files:
        bars = catalog.query(Bar, files=[file_path])
        if not bars or all(bar.ts_init == bar.ts_event for bar in bars):
            continue
        normalized_bars: list[Bar] = []
        for bar in bars:
            values = Bar.to_dict(bar)
            values["ts_init"] = values["ts_event"]
            normalized_bars.append(Bar.from_dict(values))
        interval = _parse_filename_timestamps(file_path)
        if interval is None:
            raise ValueError(f"Unrecognized Nautilus catalog filename: {file_path}")
        catalog.fs.rm(file_path)
        catalog.write_data(normalized_bars, start=interval[0], end=interval[1])


def missing_bar_intervals(
    catalog_path: Path,
    bar_type: str,
    start: datetime,
    end: datetime,
) -> list[tuple[datetime, datetime]]:
    # A Nautilus parquet catalog is rooted at ``catalog_path`` and stores its
    # datasets below ``data``.  The state root itself may already exist even on
    # a fresh installation, so treating that directory as a usable catalog
    # only defers the failure to ParquetDataCatalog with an opaque exception.
    if not catalog_path.exists() or not (catalog_path / "data").is_dir():
        return [(start, end)]
    parsed_bar_type = BarType.from_str(bar_type)
    if parsed_bar_type.spec.is_time_aggregated():
        interval = parsed_bar_type.spec.timedelta
        step_ns = int(interval.total_seconds() * 1_000_000_000)
        start_ns = int(start.timestamp() * 1_000_000_000)
        end_ns = int(end.timestamp() * 1_000_000_000)
        first_ns = ((start_ns + step_ns - 1) // step_ns) * step_ns
        last_ns = ((end_ns - 1) // step_ns) * step_ns
        if first_ns > last_ns:
            return []

        catalog = ParquetDataCatalog(str(catalog_path))
        present = {
            bar.ts_event
            for bar in catalog.bars(bar_types=[bar_type])
            if first_ns <= bar.ts_event <= last_ns
        }
        cursor_ns = first_ns
        gaps: list[tuple[datetime, datetime]] = []
        gap_start_ns: int | None = None
        while cursor_ns <= last_ns:
            if cursor_ns not in present and gap_start_ns is None:
                gap_start_ns = cursor_ns
            elif cursor_ns in present and gap_start_ns is not None:
                gaps.append(
                    (
                        datetime.fromtimestamp(gap_start_ns / 1_000_000_000, tz=UTC),
                        datetime.fromtimestamp((cursor_ns - step_ns) / 1_000_000_000, tz=UTC),
                    ),
                )
                gap_start_ns = None
            cursor_ns += step_ns
        if gap_start_ns is not None:
            gaps.append(
                (
                    datetime.fromtimestamp(gap_start_ns / 1_000_000_000, tz=UTC),
                    datetime.fromtimestamp(last_ns / 1_000_000_000, tz=UTC),
                ),
            )
        return gaps

    catalog = ParquetDataCatalog(str(catalog_path))
    intervals = catalog.get_missing_intervals_for_request(
        start=int(start.timestamp() * 1_000_000_000),
        end=int(end.timestamp() * 1_000_000_000),
        data_cls=Bar,
        identifier=bar_type,
    )
    return [
        (
            datetime.fromtimestamp(interval_start / 1_000_000_000, tz=UTC),
            datetime.fromtimestamp(interval_end / 1_000_000_000, tz=UTC),
        )
        for interval_start, interval_end in intervals
    ]
