---
name: edgepilot
description: Install, configure, backtest, and run trading strategies through EdgePilot's agent-facing CLI and native NautilusTrader engine. Use when the user wants to inspect strategy settings or presets, download historical bars/trades/quotes/order-book data, backtest instruments, compare results, query saved metrics or raw outputs, manage credentials, reproduce a run, or start paper, exchange-demo, or live trading.
---

# EdgePilot

Use the bundled `edgepilot` CLI. Do not generate ad hoc node scripts or recreate Nautilus abstractions.

## Local dashboard

The chat and bundled `edgepilot` CLI remain the primary way to configure,
backtest, and start strategies. Use the optional localhost dashboard when the
user wants to inspect charts, configure a selected strategy visually, manage
local exchange credentials, or monitor an active Demo/Live session. It reads
the same persistent run records, timeseries, fills, positions, runtime
snapshots, and PNG artifacts as the CLI; do not create a second backtest or
execution implementation for the UI.

Infer the language of the user's current dashboard-opening instruction and always put a
URL-encoded language candidate in the returned/opened URL. Use canonical
`en`, `ko`, `zh-CN`, or `zh-TW` when supported, the original BCP 47 candidate
when recognizable but unsupported, and an empty `?lang=` when it cannot be
determined. This explicit URL candidate represents the current instruction;
the dashboard handles saved and browser-language fallbacks. When starting a
new server, also pass the same candidate with `--language`.

Run at most one dashboard for a given EdgePilot state directory. Before
starting one, check whether `http://127.0.0.1:8787/api/runs` responds; if it
does, reuse that dashboard and open `http://127.0.0.1:8787/?lang=<candidate>`.
Otherwise start exactly one localhost-only server, for example:

```bash
edgepilot ui --host 127.0.0.1 --port 8787 --language zh-CN
```

Start that command in a persistent execution session and keep it running while the user
completes browser authentication; do not treat opening the browser as completion. After the
hosted page reports success, run `edgepilot auth status` until it reports authenticated, expired,
or the login deadline is reached, then report the result. Do not call the Dashboard's protected
write endpoint without its same-origin CSRF context. If the localhost process exited, restart the same Dashboard
on port 8787 and tell the user to retry login; never silently switch ports or start a second
instance. The hosted success page cannot safely redirect to an arbitrary localhost URL.

Never start Vite, Node.js, or a separate frontend process for an end user. The
production dashboard is bundled into the plugin and served by this same Python
command. The browser page refreshes local run state every two seconds. Start
Demo or Live from the selected strategy configuration's **Deploy** dialog;
the **Live** page is monitor-only and provides an **Emergency stop** control
for an active local node. It requests a native graceful stop: the strategy
cancels its own open orders and submits reduce-only exits for its positions
before the node stops. This requests flattening; it cannot guarantee fills if
the exchange is unavailable, rejects an order, or its market is halted.
Do not claim an external session is running merely because an old run record
exists: the dashboard verifies the native process ID and shows only `RUNNING`
or `STOPPED`.

The Marketplace tab is not a local run view. It searches EdgePilot's public
cloud catalog by research terms, asset, venue, data type, and published
backtest metric. No marketplace sign-in is required to search or install.
Its **Find the right strategy** popup is a three-step guide: select one of
three risk profiles, select one of three USD allocation ranges, then review
and install a single recommendation. It filters by the publisher's declared
capacity and ranks matching packages by published Sharpe. If nothing matches,
it falls back to the marketplace momentum strategy. This is a convenience
filter, not a claim of suitability or future returns.
Installing a package downloads its selected immutable version ZIP and extracts
it safely into the local persistent `strategies/` directory; no marketplace,
exchange, or cloud credential is stored in the plugin cache. Treat installed
strategy code as code: inspect it before enabling it for a backtest or trading.
The Marketplace lists one card per strategy and defaults to its newest
published version. Select any published version explicitly to install or
update that same local package. Updating replaces the package-owned
`configs/default.json`, preserves user-created named configurations and
`runs/`, and must never occur while that strategy has an active Demo or Live
session.

Agents can search or install without operating the dashboard:

```bash
edgepilot marketplace search "BTC mean reversion" --venue BINANCE --sort sharpe
edgepilot marketplace inspect rsi2-mean-reversion --version 1.0.0
edgepilot marketplace install rsi2-mean-reversion --version 1.0.0
edgepilot strategies remove rsi2_mean_reversion --confirm
```

`strategies remove` deletes that local strategy package and its local
configurations and runs. It refuses to remove a strategy with an active
Demo/Live run. It deliberately leaves shared catalog market data intact.

## Agent workflow: discover to run

When a user asks for a strategy, own the complete CLI workflow. Do not ask
them to navigate files or operate the dashboard unless they want its charts.

1. **Search and recommend.** Search with the user's terms and relevant
   filters, then compare published period, venue, assets, return, drawdown,
   Sharpe, data type, risk profile, capacity, and strategy description. `recommend` is an agent
   decision based on these real catalog fields, not a separate opaque command.
   Use `--sort return`, `--sort drawdown`, or `--sort sharpe` only as a
   starting order; never recommend from a single metric alone. If the user
   has not given a search constraint, ask for a risk preference—**Conservative
   (稳健)**, **Balanced (平衡)**, or **Aggressive (激进)**—and a
   minimum capacity. If the user already says what they want, search directly.
2. **Inspect, then install an exact version.** Read the strategy description,
   configuration schema, packaged backtest period/results, and package files
   before installing. Install only the version the user selected.
3. **Inspect settings and resolve defaults.** Run `strategies inspect` and
   `strategies presets`. Explain strategy-authored settings and the selected
   preset's defaults. Ask only for required settings or meaningful choices the
   user has not provided.
4. **Change settings correctly.** Use `backtest --set KEY=VALUE` for a
   one-off native strategy parameter change. For a reusable combination of
   strategy, market, venue, fee, leverage, or backtest settings, create a
   named JSON preset in that strategy's `configs/` directory. Never change
   strategy source merely to use another asset, venue, date range, fee, or
   run mode.
5. **Run and explain the result.** Run a native backtest, read its saved
   metrics and raw artifacts, and report assumptions. Each result is saved in
   that strategy's `runs/RUN_ID/` and can be reproduced or promoted unchanged
   to Paper, Demo, or Live.
6. **Run safely.** Use `paper` for local simulated execution, `demo` for an
   exchange demo account, and `live --confirm-live` only after an explicit
   confirmation. Check exact mode-scoped credential requirements first.

Typical chat-only sequence:

```bash
edgepilot marketplace search "ETH momentum" --venue BINANCE --sort sharpe
edgepilot marketplace search --risk-profile balanced --min-capacity-usd 100000
edgepilot marketplace inspect bollinger-momentum --version 1.0.0
edgepilot marketplace install bollinger-momentum --version 1.0.0
edgepilot strategies inspect bollinger_momentum
edgepilot backtest bollinger_momentum --preset default --set bollinger_k=2.8
edgepilot runs show RUN_ID
edgepilot demo --run RUN_ID --dry-run
```

Never embed any marketplace administration token in the plugin or ask the user
to paste one into chat. Publishing remains administrator-only.

## Set up

Use the bundled code from this skill directory, but keep user-owned state outside the plugin cache.
The user only needs to ask in chat; perform installation in the terminal for them. First detect the
operating system and whether CPython 3.12 is available to bootstrap the installer script.
If it is missing, install Python 3.12 with the platform's normal package manager:

- macOS with Homebrew: `brew install python@3.12`;
- Windows with WinGet: `winget install --exact --id Python.Python.3.12`;
- Debian/Ubuntu: install Python 3.12 and its `venv` package from the configured package source.

If the package manager is unavailable or requires a graphical installer, explain the single
required installation step and resume after it completes. Never ask the user to install individual
Python libraries. Prefer the bundled cross-platform installer
(`skills/edgepilot/scripts/install_runtime.py`). It uses **uv** to install the Python version
required by the hosted wheel (from `manifest.json`), creates a local venv, installs the
**prebuilt** `nautilus_trader` wheel from the marketplace runtime host (or an explicit local
wheel path for smoke tests), then installs the plugin editable. It does **not** vendor or
compile Nautilus. EdgePilot Live and its prebuilt Nautilus wheel both use CPython 3.12.
The installer always builds a separate relocatable candidate environment, runs `pip check`,
imports EdgePilot, its packaged backtest core, NautilusTrader, and the required Live adapters,
and exercises the CLI before activation. It then swaps the candidate into the stable `.venv`
path, keeps the prior environment as `.venv.previous`, and restores it if the post-activation
check fails. Never install an upgrade directly into the active environment.

```bash
# plugin-root = directory that contains edgepilot/pyproject.toml (the extracted plugin)
python3 <plugin-root>/skills/edgepilot/scripts/install_runtime.py <plugin-root>
```

On Windows use `py -3` or `python` instead of `python3` when needed. The installer selects
`%APPDATA%\\EdgePilot\\.venv` automatically.

Wheel sources (first match wins):

1. `--wheel` / `EDGEPILOT_NAUTILUS_WHEEL` — already-built local `.whl` (internal smoke test only);
2. `--wheel-url` / `EDGEPILOT_NAUTILUS_WHEEL_URL`;
3. `--wheel-base-url` / `EDGEPILOT_NAUTILUS_WHEEL_BASE_URL` / the default EdgePilot runtime
   host: download
   `{base}/manifest.json` and pick the matching platform wheel (preferred), otherwise try
   known filename candidates; checksum comes from the manifest or `--sha256`;
4. otherwise `pip` may satisfy `nautilus_trader==…` from PyPI via `pyproject.toml` (transition only).

The wheel version must match the pinned `nautilus_trader==…` in `pyproject.toml`. The installer
installs the custom wheel first, then resolves the plugin's remaining dependencies; pip therefore
keeps the already-installed matching custom build instead of replacing it with the public package.

Runtime wheel coverage may be Linux-only until macOS/Windows artifacts are published. If
`manifest.json` has no match for the current OS, report that clearly instead of inventing a build.

Verify a fresh installation before doing anything else:

```bash
~/.edgepilot/.venv/bin/edgepilot --help
~/.edgepilot/.venv/bin/edgepilot strategies list
```

Do not expect `.env`, `catalog/`, strategy packages, or run records in the plugin cache. EdgePilot creates them only in
the stable user state directory when credentials, downloaded data, or runs are needed.

## Install and inspect strategies

Treat a downloaded strategy as strategy code, not as a new runtime. Do not add adapter, backtest,
paper, demo, live, credential, fee, or data-download code to it.

For every local or community strategy:

1. Review its source and declared dependencies before executing it.
2. Place it under `~/.edgepilot/strategies/STRATEGY_NAME/` as a Python package. Install only additional
   libraries the strategy actually imports, preferably from its declared package metadata or
   requirements. Keep each strategy in its own package directory.
3. Verify that the module imports successfully and defines exactly one native Nautilus `Strategy`
   subclass and one native `StrategyConfig` subclass.
4. Run `edgepilot strategies inspect STRATEGY_NAME` and read the native configuration schema and
   available named presets.
5. Tell the user what the strategy does, then show a concise table containing every
   strategy-authored setting, its type, its default, whether it is required, and a plain-language
   meaning. Do not dump the raw JSON schema unless requested. Do not list inherited Nautilus
   operational fields unless the strategy changes them or the user asks about them.
6. Ask only for required values with no defaults and any choices the user's request has not already
   answered. Clearly state which defaults will be used; do not make the user re-enter defaults.
7. Validate the resolved strategy configuration before downloading data or starting a node.

```bash
~/.edgepilot/.venv/bin/edgepilot strategies list
~/.edgepilot/.venv/bin/edgepilot strategies inspect bollinger_momentum
~/.edgepilot/.venv/bin/edgepilot strategies presets bollinger_momentum
```

Marketplace listings and versions can change. Search the catalog instead of
assuming a particular strategy is installed. Current research listings include
strategies such as:

- `bollinger_momentum`: hourly Bollinger breakout momentum with an optional Ichimoku filter;
- `rsi2_mean_reversion`: Jesse/Connors-style RSI(2) mean reversion with a slow SMA trend filter
  and fast SMA exit.

The RSI(2) strategy is included as a research example, not as a claim of profitability. Validate
it across assets, dates, and the correct venue fees before paper or live use.

Risk profiles are stored as `conservative`, `balanced`, and `aggressive`; the
CLI also accepts the Chinese aliases `稳健`, `平衡`, and `激进`. Risk profile and
capacity are publisher-supplied version metadata. Capacity is
only a demo/research estimate; absent capacity means not assessed, never
unlimited.

Use this package structure:

```text
strategies/STRATEGY_NAME/
├── __init__.py
├── strategy.py
├── configs/
│   ├── default.json
│   └── another-market.json  # optional future preset
└── runs/
    ├── PUBLISHED_RUN_ID/      # complete publisher backtest artifact
    └── RUN_ID/                # local backtest, paper, demo, or live result
```

Every strategy owns its own run records; never create a global `runs/`
directory. A packaged benchmark is a publisher-provided backtest record under
that strategy's `runs/` folder. The dashboard uses these records to filter and
sort installed strategies by return, drawdown, or Sharpe. It must be produced
by the normal native backtest command and include its fills, positions,
timeseries, and chart artifacts; never hardcode its metrics. Keep downloaded
market data shared under `~/.edgepilot/catalog/`.

Require exactly one native `Strategy` subclass and one native `StrategyConfig` subclass. The
bundled strategy currently has one preset: `bollinger_momentum/configs/default.json`. A strategy
may gain additional named presets later. Each preset may contain:

- `strategy`: native strategy settings shared by backtest, paper, demo, and live;
- `backtest.markets`: every market leg, each with `instrument_id`, `bar_type`, `venue`, and `data_type`;
- `backtest.venues`: one native adapter/account configuration per venue, including venue-specific
  fees, balances, non-secret adapter settings, and optional leverage/margin controls.

Keep credentials out of presets. Resolve settings in this order: explicit CLI override, selected
preset, then EdgePilot's built-in default. Save the fully resolved settings into each run record.
The Marketplace research defaults use ETH perpetuals on Binance USDⓈ-M Futures. Never edit the CLI to register a
strategy or preset.

Changing markets, venues, bar sources, backtest period, fees, or run mode changes the native
configuration, not the strategy implementation. Use the same strategy class for backtest, paper,
demo, and live trading. A run may contain any number of markets and venues.

## Create configurations and strategies

When the user wants the same trading logic with different settings, create a preset, not a new
strategy. Copy that strategy's `configs/default.json` to a short descriptive JSON filename, change
only the necessary values, validate it by inspecting the strategy and loading the preset, then run
a dry run or backtest. Keep the strategy and backtest objects, with every market and venue declared explicitly:

```json
{
  "strategy": {
    "instrument_id": "ETHUSDT-PERP.BINANCE",
    "bar_type": "ETHUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL",
    "signal_bar_spec": "1-HOUR-LAST-INTERNAL",
    "bollinger_period": 40,
    "bollinger_k": 3.0,
    "ichimoku_tenkan": 9,
    "ichimoku_kijun": 26,
    "ichimoku_senkou": 52,
    "ichimoku_displacement": 26,
    "use_ichimoku": true,
    "notional_fraction": 0.95,
    "allow_short": true,
    "warmup_days": 5
  },
  "backtest": {
    "days": 365,
    "markets": [
      {
        "instrument_id": "ETHUSDT-PERP.BINANCE",
        "bar_type": "ETHUSDT-PERP.BINANCE-1-HOUR-LAST-EXTERNAL",
        "venue": "BINANCE",
        "data_type": "bars"
      }
    ],
    "venues": {
      "BINANCE": {
        "starting_balance": 100000,
        "base_currency": "USDT",
        "account_type": "USDT_FUTURES",
        "oms_type": "NETTING",
        "default_leverage": 1.0,
        "allow_cash_borrowing": false,
        "liquidation_enabled": false
      }
    },
    "download": true,
    "export_artifacts": true
  }
}
```

Do not blindly copy adapter settings across venues; inspect the target native adapter and use its
configuration fields. Keep API keys, secrets, and passphrases in `.env`, never in a preset.

When the user wants different entry, exit, sizing, or risk logic, create a new strategy package:

1. Copy the package shape, not the Bollinger implementation.
2. Define exactly one native Nautilus `StrategyConfig` and one native `Strategy` in `strategy.py`.
3. Re-export both classes from `__init__.py`.
4. Add `configs/default.json` with `strategy` and `backtest.markets`/`backtest.venues` objects.
5. Install only any genuinely new declared dependency.
6. Run `edgepilot strategies list`, `edgepilot strategies inspect NAME`, and a backtest.

Discovery is automatic; never add a registry entry or mode-specific implementation. The new native
strategy automatically uses the existing download, backtest, paper, demo, live, fee, credential,
reporting, and run-history flows.

## Download data

Use an installed Nautilus adapter. Declare every market and venue in the preset; the framework
downloads each market with its venue's native adapter.

```bash
~/.edgepilot/.venv/bin/edgepilot data pull \
  --venue OKX \
  --instrument BTC-USDT-SWAP.OKX \
  --data-type bars \
  --bar-type 1-MINUTE-LAST-EXTERNAL \
  --days 365 \
  --adapter-set 'instrument_types=["SWAP"]'
```

Use the exact request names `bars`, `trades`, `quotes`, `order-book-depth`, and
`order-book-deltas`. Actual historical availability depends on the native adapter. Let a native
unsupported-request error reach the user; do not synthesize unavailable data.

## Backtest

Use `--set` for native strategy configuration fields. Inspect the strategy first when fields are unknown.

```bash
~/.edgepilot/.venv/bin/edgepilot backtest bollinger_momentum \
  --preset default
```

Load `default` automatically when the strategy provides it. The selected preset supplies all
market legs and venue configurations; `--set` overrides native strategy fields only.

Read backtest defaults from the preset's `backtest` object. Use a rolling `days` period unless the
user supplies exact `--start` and `--end` timestamps. The command downloads missing bars when
`download` is enabled, runs native `BacktestNode`, and writes:

- `run.json`: complete reproducible configuration plus headline metrics;
- `metrics.json`: precomputed metrics for quick agent queries;
- `fills.csv` and `positions.csv`: raw trading results;
- `timeseries.json`: price, mark-to-market PnL, equity, and drawdown by bar;
- `backtest.png`: one indexed-price panel per market with entries/exits, plus portfolio PnL and
  equity panels.

Report total return, realized PnL, maximum drawdown, Sharpe, Sortino, win rate, profit factor,
orders, and positions. State fees, data, and bar-execution assumptions. Never describe historical
profit as guaranteed.

Long OKX bar downloads show bars received, the earliest timestamp reached, throughput, and ETA. The implementation follows Nautilus's pinned OKX integration test: cache native instruments, request pages of at most 100 bars, and advance using returned event timestamps.

Default each market to the base maker/taker fees carried by its venue's native Nautilus instrument
definition. A `maker_fee_bps`/`taker_fee_bps` override belongs inside that venue's configuration.
Always persist the effective rates for every venue.
Optional Nautilus margin controls belong in the same venue object: `default_leverage` (default
`1.0`), `leverages` (per-instrument overrides), `allow_cash_borrowing` (default `false`), and
the liquidation fields. If omitted, EdgePilot passes NautilusTrader's defaults unchanged.
The bundled defaults use ETH perpetuals on Binance USDⓈ-M Futures and rely on the native Binance
instrument fee metadata. Override venue fees when the account tier differs.

## Paper, demo, and live trading

The three modes are deliberately separate:

- `paper` uses Nautilus's local sandbox matching engine with live market data. Orders and fills are simulated locally and it never places exchange orders. The selected native data adapter may still require read-only API credentials; pinned OKX requires them for its business WebSocket bars.
- `demo` uses the exchange adapter's native demo/test environment. It places orders in the exchange's demo account and requires that environment's API credentials.
- `live` uses the exchange adapter's production environment. It places real orders, requires production credentials, and requires explicit confirmation.

Before starting a mode, inspect the saved run's venues and check each native adapter's credential
requirements:

```bash
~/.edgepilot/.venv/bin/edgepilot credentials check --run RUN_ID --mode paper
~/.edgepilot/.venv/bin/edgepilot credentials check --run RUN_ID --mode demo
~/.edgepilot/.venv/bin/edgepilot credentials check --run RUN_ID --mode live
```

For a strategy preset that has no saved run yet, check it directly:

```bash
~/.edgepilot/.venv/bin/edgepilot credentials check \
  --strategy bollinger_momentum \
  --preset default \
  --mode demo
```

If any requirement reports `required: true` and `configured: false`, ask the user for only those values. Write them to
the user-state `~/.edgepilot/.env` using the reported mode-scoped environment-variable names, set owner-only file
permissions where supported, and never repeat credential values in the response. The CLI loads
`~/.edgepilot/.env` automatically. Different adapters can require different fields; never hardcode an OKX
credential prompt for every adapter.

Keep credential sets isolated by mode. For example, the native OKX fields map to
`OKX_PAPER_API_KEY`, `OKX_DEMO_API_KEY`, or `OKX_LIVE_API_KEY` and their corresponding secret and
passphrase variables. The CLI passes the selected set directly into the native Nautilus config;
never copy one mode's values into Nautilus's unscoped fallback variables.

Promote an exact saved run so the strategy class and parameters remain unchanged:

```bash
~/.edgepilot/.venv/bin/edgepilot paper --run RUN_ID
~/.edgepilot/.venv/bin/edgepilot demo --run RUN_ID
~/.edgepilot/.venv/bin/edgepilot live --run RUN_ID --confirm-live
```

New runs already contain every selected market and each venue's non-secret native adapter settings.

Alternatively, start directly from a named strategy preset. Persist the fully resolved
configuration as a new run when the node actually starts:

```bash
~/.edgepilot/.venv/bin/edgepilot paper --strategy bollinger_momentum --preset default
~/.edgepilot/.venv/bin/edgepilot demo --strategy bollinger_momentum --preset default
~/.edgepilot/.venv/bin/edgepilot live --strategy bollinger_momentum --preset default --confirm-live
```

Use exchange credentials from environment variables for `demo` and `live`. Always request explicit user confirmation immediately before starting live trading. Use `--dry-run` to validate resolution without connecting.

List saved runs and their current process status with:

```bash
~/.edgepilot/.venv/bin/edgepilot runs list
~/.edgepilot/.venv/bin/edgepilot runs status RUN_ID
```

Status is intentionally only `RUNNING` or `STOPPED`. The CLI stores run-ID-to-PID mappings and
checks whether each PID still exists. Missing processes are marked stopped and removed from the
running list; do not invent heartbeat or connection-state layers.

## Native boundaries

- Keep strategy logic independent of venues, credentials, fees, catalogs, dates, and run modes.
- Use Nautilus `ImportableStrategyConfig`, `BacktestNode`, `TradingNode`, native adapters, native catalog objects, analyzer, and reports.
- Do not add exchange adapters, strategy registries, deployment layers, runtime frameworks, or alternate data and fee models.
- Store generated state only in the stable user state directory (`~/.edgepilot/` on macOS/Linux or
  `%APPDATA%\\EdgePilot\\` on Windows; `EDGEPILOT_HOME` overrides it), including all strategies,
  their configs and runs, credentials, and downloaded data.
- When developing against this git checkout, set `EDGEPILOT_HOME` to the repository root so the
  CLI reads `strategies/` here. If there is no Marketplace session, also set
  `EDGEPILOT_SKIP_AUTH=1`; otherwise `strategies inspect`, `data pull`, and `backtest` block on
  device login and never start. Do not set `EDGEPILOT_SKIP_AUTH` for a normal user install. The
  flag does not grant Marketplace install or admin rights. Release backtests use
  `MARKETPLACE_ADMIN_TOKEN`.
