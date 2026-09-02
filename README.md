# EdgePilot

EdgePilot is a local-first trading workflow plugin. It bundles a Python CLI,
native NautilusTrader integration, a local dashboard, and one universal
`skills/edgepilot/SKILL.md` workflow. Runs are multi-market and multi-venue by
default: each preset declares its market legs and venue configurations.

The repository-wide publication, catalog, recommendation, installation,
backtest, and paper/demo/live state machine is defined in the repository-root
authority at `docs/strategy-catalog-search-recommendation.md`.
This README remains the Live user and packaging guide.

## Install with Codex

When you attach this extracted plugin directory to a Codex task, send the whole
request below. The second sentence must be part of your request because files
attached to a task provide context; they do not become user instructions by
themselves.

```text
Install this EdgePilot Live plugin. After installation, read the "Post-install guidance" section in README.md. If this Codex surface can create tasks, create and start the guided first-use task with its localized prompt; otherwise include that prompt in your final response.
```

The lightweight local MCP connects to the single-instance Live service on the first Dashboard operation; `edgepilot ui` and the browser use that same service without native dependencies. Chat, browser handoff and browser heartbeat all use one typed expiring-lease model; active jobs keep the service alive independently of a Codex chat. Starting the MCP protocol itself does not start the Dashboard service. The agent creates a verified virtual environment and installs native dependencies only on the first confirmed runtime-dependent operation or after its Python/native dependency contract changes. Ordinary MCP, UI and plugin-code upgrades reuse that native runtime and update only the installed EdgePilot package through a verified candidate once the runtime is in the stable root; the first Windows upgrade from the former AppData runtime installs a fresh candidate instead.

The Codex plugin exposes one first-use route: verify activation, collect one
preference at a time, wait for confirmation, render three recommendation cards,
and then open the Live Dashboard from its verified MCP URL. Catalog listing and backtesting remain
available after onboarding, but they are not installation validation steps.
The bundled recommendation tool uses the existing anonymous Live recommendation
endpoint, so the seven answers and three cards do not require login. Installing a
selected package and using account or trading capabilities still require the
applicable authenticated session and confirmation.

The Live monitor enriches each open position with mark-to-market unrealized P&L
from NautilusTrader's cached quote when one is available. A missing quote leaves
that position's value unavailable without hiding the position or other runtime
reports; account-level unrealized P&L remains the venue-reported aggregate.

The plugin cache contains no credentials or trading data. User strategies, market
data, credentials, runs, and Dashboard service state are stored in
`~/.edgepilot/` on every supported platform (on Windows this is
`%USERPROFILE%\\.edgepilot`), with `EDGEPILOT_HOME` available as an override.
This keeps the path stable when a Microsoft Store host virtualizes `AppData`;
the Windows compatibility and legacy-state boundary is documented in
`../docs/windows-live-path-compatibility.md`. Signed-in users keep installed
strategies, exchange credentials, and runs under an account-specific
`accounts/<non-PII-key>/` directory derived from the Marketplace user ID.
Market data and the locked runtime remain shared because they contain no account
orders or secrets.

On the first Windows upgrade from the former `%APPDATA%\EdgePilot` root, the
maintenance entry point safely stops a service verified from that old root and
moves authentication metadata, accounts, strategies, runs and catalog into
`%USERPROFILE%\.edgepilot`. Each entry is renamed atomically on the user-profile
volume; an interruption resumes from a local migration record and a normal
failure rolls completed entries back. The migration never merges with existing
new-root user state and never moves the separate locked runtime, logs or service
records. The old runtime remains untouched but is not reused; the next confirmed
runtime operation installs it under the stable profile root. A configured
`EDGEPILOT_HOME` remains an explicit opt-out.

EdgePilot Live has one fixed localhost address: `http://127.0.0.1:8787`.
It never falls back to a random port. Install, repair, upgrade and uninstall
first stop only services authenticated by EdgePilot's private service records in
the current root and, during the Windows path transition, the former AppData root;
active runs and jobs block maintenance, while an unfinished login can be
restarted after activation. A different program on 8787 is reported as a port
conflict and is never terminated automatically.

The published Live runtime supports Apple Silicon Macs (`arm64`) and 64-bit
Windows (`amd64`), both with CPython 3.12. Intel Macs and Linux are not supported.
An Intel Mac is rejected before any runtime download or state change; an Apple
Silicon Mac running through Rosetta is told to retry from a native arm64 process.

The local `strategies/` directory starts empty. Install a reviewed package from
the Marketplace, or add a custom strategy package there; both survive plugin
updates.

Each strategy owns its configurations and results:

```text
~/.edgepilot/accounts/ACCOUNT_KEY/strategies/STRATEGY_NAME/
├── strategy.py
├── configs/
└── runs/
    ├── PUBLISHED_RUN_ID/       # complete publisher backtest artifact
    └── 20260805-.../           # later local backtest, paper, demo, or live run
```

There is no global runs directory. Market data remains shared in
`~/.edgepilot/catalog/` so strategies can reuse the same downloaded data.
Published records are generated by the same native backtest command as local
records, including fills, positions, timeseries, and the PNG chart.

Backtests automatically download missing bars before execution. On Windows, the writable catalog
used temporarily for fee overrides is created under the system temporary directory and removed
after the run, avoiding the deeply nested account/strategy/run path; macOS and Linux retain the
run-local temporary catalog. Legacy presets may still contain
`backtest.download`, but the field no longer disables automatic data preparation. Normal installs use
`~/.edgepilot/catalog/` on every supported platform. Repository
development with `EDGEPILOT_HOME="$PWD"` instead uses `$PWD/catalog/`; do not copy market data into
the plugin or strategy package. Binance Futures reports kline request bounds by open time while
EdgePilot catalogs external bars by canonical close time. EdgePilot therefore shifts a missing
close-time interval back by one bar for the native request and filters the response back to the
requested close boundaries; a missing bar exactly on an hour can be filled without silently using
the following bar. Native Binance HTTP clients receive the process-local standard `HTTPS_PROXY` or
`https_proxy` value when present; EdgePilot does not persist that value or define a proxy address.

## Repository strategies

The repository-level `../strategies/` directory contains strategy source
packages and their publisher benchmark runs, including:

- `bollinger_momentum`
- `rebound_confirm`
- `rsi2_mean_reversion`

They are not automatically copied into the shareable plugin ZIP. A user starts
with no installed strategies and uses Marketplace or an agent to inspect and
install an exact strategy version.

## Source development setup (not Codex plugin installation)

Do not run the commands in this section during a normal Codex plugin install or
upgrade. They are only for contributors running the repository source directly.
Normal plugin delivery must not create a Python environment, install
NautilusTrader, query the authenticated strategy catalog, or use runtime health
as an installation success condition.

For source development, the Python environment is user-owned, outside both the
plugin and strategy source directories:

Installing or upgrading the plugin updates files on disk but cannot hot-replace
an MCP process in an already open task. After installation, open a new Codex task.
When the verified service preflight reports `not_running` or safely stops the old
service and the new task's `verify_activation` result is `ready`, no full Codex restart is required. Restart Codex only if a verified old service remains, the
new task does not expose the installed MCP, or the reported MCP version differs
from the installed version. The package `BUILD.json` exposes this conditional
`host_activation` contract. Upgrade and repair preserve `~/.edgepilot` (Windows:
`%USERPROFILE%\\.edgepilot`). On Windows, this release and later releases install the locked
runtime in that stable profile root instead of reusing the former
`%APPDATA%\\EdgePilot` runtime; the legacy bytes remain untouched unless the user
explicitly requests complete data removal. Before replacing plugin files, the
agent runs the candidate package's verified service stopper:

```bash
python3 <plugin-root>/skills/edgepilot/scripts/stop_local_service.py --force
```

On Windows use `py -3` or `python`. The helper refuses to stop active work and
never terminates a process merely because it owns port 8787.

```bash
mkdir -p ~/.edgepilot
python3 -m venv ~/.edgepilot/.venv
~/.edgepilot/.venv/bin/python -m pip install --upgrade pip
~/.edgepilot/.venv/bin/python -m pip install -e /path/to/strategy-agent/edgepilot
~/.edgepilot/.venv/bin/edgepilot --help
```

On Windows, create `%USERPROFILE%\\.edgepilot\\.venv` with `py -m venv`, install
the `edgepilot` directory with that environment's `pip`, then use
`%USERPROFILE%\\.edgepilot\\.venv\\Scripts\\edgepilot.exe`.

`pip install -e` is an editable install: the virtual environment uses the
Python source at the given plugin path, rather than copying it into the
environment. For repository development, install `../edgepilot` once and use the
normal `edgepilot` command. `PYTHONPATH=/path/to/edgepilot/src` is only a
temporary alternative for running uninstalled source directly; end users and
agents do not need it.

### Post-install guidance

After installation, reply in the user's current conversation language. When the
current Codex surface exposes a task-creation tool, the installation request above
is explicit authorization to create and start a fresh task, using the same project
when applicable, with the localized prompt below as its initial message. Open that
task for the user when the host supports navigation. Do not use task handoff: it
moves an existing task and does not create the fresh MCP/tool context required after
installation. If task creation is unavailable, tell the user to create a new Codex
task and include the prompt for one-click copying. No restart is needed when the
verified service preflight reports `not_running` or `stopped`; restarting Codex is
only the fallback when the new task cannot verify the installed MCP version.
Translate the following prompt naturally while
preserving the exact `@EdgePilot` mention and its request to ask about preferences
one question at a time, obtain confirmation, show three recommendation cards, and
open the EdgePilot Live Dashboard after the cards:

```text
@EdgePilot Help me choose a suitable trading strategy. Ask about my preferences one question at a time, then show three strategy recommendation cards after I confirm them. Please open the EdgePilot Live Dashboard at the same time.
```

The new task completes the questionnaire first. After confirmation it renders the three recommendation cards and calls `open_dashboard` directly with the same `locale` used for `recommend_strategies`, so the verified URL opens the Dashboard in that language. The user does not need to send another Dashboard command. If the host blocks automatic navigation, the tool result still provides the verified clickable URL. The onboarding flow does not install or rebuild the native runtime.

## Use

Ask the agent to list strategies, inspect a preset, download data, run a
backtest, or start paper/demo/live trading. Credentials are requested only for
the selected exchange mode.

Longbridge uses authenticated quote data in all three trading modes. Its
`paper` mode keeps execution in the local Nautilus sandbox, `demo` targets the
official Longbridge simulated account, and `live` targets the real account;
each mode uses its own App Key, App Secret, and Access Token variables stored in
the selected EdgePilot account directory.

For a visual local monitor, run `edgepilot ui`. It discovers or starts the same
localhost-only service used by the MCP and prints its verified URL; it never
binds a second Dashboard or falls back from port 8787.
It reads the same records as the CLI and lets users configure a strategy,
start a backtest, inspect interactive equity/price charts and entry/exit
markers, manage local credentials, and monitor active Demo/Live sessions.
Email-code login stays on the Dashboard page. Google sign-in opens the system
browser, completes hosted Google OAuth against the Dashboard's existing device
authorization, and remains on the hosted completion page. Python polls that device
authorization and stores only the resulting EdgePilot token family; Google tokens
are not retained locally. The original Codex Dashboard detects the local session and
enters the authenticated state automatically. Both methods share the same account whenever the Marketplace
resolves the same normalized email to one canonical user ID. Signing out revokes the remote session and
hides that account's local data; active runs and jobs must be stopped first.
Older unassigned `~/.edgepilot/strategies/` and `~/.edgepilot/.env` data is not
silently attached to the first new login: the Account page offers an explicit,
one-time assignment when the destination account is empty.
The installed-strategy page filters by asset and sorts the packaged/local
backtest records by return, drawdown, or Sharpe.
The **Marketplace** tab is separate from local state: it searches the public
EdgePilot cloud catalog by research terms, asset, venue, data type, and
published backtest metrics, then downloads a selected immutable package version
into the local `strategies/` directory. This Live Marketplace client requires
an EdgePilot account and a token with `marketplace:install` permission. The
separate Research client remains account-free and does not send a token or
machine identifier, although the cloud service records the full client IP for
Research downloads.
Marketplace pagination defaults to 15 strategies per page and lets users select
15, 30, 50, or 100. Changing the page size, search, sort, or compatible-exchange
filter returns to page one; search and filter changes retain the selected page
size for the current Dashboard session. Live and Research use the same controls
and option set, while keeping their independent localhost clients and state.
Marketplace packages are reviewed and installed as code locally, while their
cloud metadata and ZIP remain in the marketplace service. Agents can use the
same catalog with `edgepilot marketplace search`, `inspect`, and `install`.
Strategy ZIP downloads are limited in UTC to 20 per day and 100 per month.
Research and Live share the source-network allowance (IPv4 address or IPv6
`/64`); Live also consumes the same daily and monthly allowance for the signed-in
account. Search, inspection, backtests, trading modes, and runtime-wheel
downloads do not consume this allowance. An exhausted allowance is not retried
automatically. `marketplace restore` keeps completed restores and stops at the
first quota error; an exact version already installed locally is never
downloaded again.
The Marketplace also includes a **Find the right strategy** page. Its guided
questionnaire collects seven trading preferences plus optional review-only
context, then compares the answers with published evidence and shows three
distinct choices: best fit, relatively steadier, and more aggressive. The
optional context is not submitted or used for ranking. Selecting a choice
installs or updates that exact immutable package version when necessary, then
opens its default strategy workspace so the user can review the configuration
and start a backtest. EdgePilot does not silently substitute a default strategy
or automatically run the backtest. Recommendations and backtests are research
tools, not investment advice or promises of future performance.
Demo or Live is started from a strategy configuration's **Deploy** dialog;
the **Live** page monitors and stops active local nodes. The dashboard is a
view/control layer; NautilusTrader and the EdgePilot CLI remain the execution
path.

The dashboard frontend is bundled into the plugin. End users do not need
Node.js or a separate frontend process. From this `edgepilot/` directory, use the
following only for frontend development:

```bash
cd ui
npm install
npm run dev
```

`npm run build` writes an ignored local production bundle to
`src/edgepilot/ui_assets/app/`, where the local Python process can serve it.
Do not commit that directory. Formal packaging installs the locked frontend
dependencies and rebuilds the bundle in a temporary directory before adding it
to the plugin archive and wheel.
