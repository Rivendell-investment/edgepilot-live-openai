---
name: edgepilot
description: Route strategy discovery, configuration, backtesting, demo/live trading instances (deploy, stop, resume, flatten) and failure diagnosis (why an install, backtest, trading instance or request failed) through the local EdgePilot Runtime Host. Use for EdgePilot Live workflows; never expose credentials or bypass confirmation gates.
---

# EdgePilot Live router

The Node Ready Bridge always exposes `edgepilot_runtime_status`,
`edgepilot_runtime_start`, `edgepilot_runtime_update` and `edgepilot_runtime_repair`, even
before Runtime exists. The bridge automatically prepares the release-bound Runtime before first business use; never treat an older compatible Runtime as ready. Call status when Host tools are unavailable, then start once. When
Runtime is ready, use the five Host meta tools:

1. `edgepilot_connection_list`
2. `edgepilot_tool_search`
3. `edgepilot_tool_get`
4. `edgepilot_tool_execute`
5. `edgepilot_result_present`

Search for an operation, fetch its exact current descriptor, then execute with the returned
`schema_revision`. Do not invent dynamic MCP tools or cache operation schemas in the
plugin. Strategy package and configuration digests returned by the Runtime must remain
unchanged through backtest or execution requests.

Route one user outcome at a time:

- account availability goes directly to `edgepilot_connection_list`;
- unknown capabilities use one concise English `edgepilot_tool_search` query, with named
  toolkits as filters and no execution values in the query;
- retrieve up to eight exact contracts in one `edgepilot_tool_get` call;
- batch only independent calls in `edgepilot_tool_execute`; a value returned by an earlier
  call starts a later batch;
- use `presentation: "if_required"` normally and present only an execute-minted
  `result_ref`.

Route every ordinary chat request for finding or recommending strategies through
`edgepilot_strategy_search`, including identity/keyword lookup, explicit hard filters,
subjective fit, mixed preferences, “recommend a low-risk strategy” and “find something for
small capital”. Preserve the user's locale and every supported hard constraint; keep
unsupported wishes in the natural-language query and disclose constraints the owner cannot
apply. Never call `edgepilot_strategy_recommend` or discover/execute
`catalog.strategy.recommend` for an ordinary chat request, and never create a V3
questionnaire payload. Only an explicit request to open/start the questionnaire or strategy
onboarding may enter the onboarding flow below. The onboarding App, or its explicitly
requested textual fallback, submits the complete confirmed V2 questionnaire. Preserve the
owner order and never merge repeated searches into a new owner ranking. If the user asks for an exact number of recommendations, send that number as
`limit` (for example, `limit=1`, `limit=2` or `limit=3`); do not let the search tool default
to ten results for a counted request. Use a larger explicit limit only when the user asks for
options or multiple candidates. For “open”, “start” or “launch EdgePilot”, ensure Runtime is ready, then
call `edgepilot_dashboard_open`; present its URL as a link (Dashboard links below) and never
spawn a legacy Dashboard directly.

## Dashboard links

Show a Dashboard URL as one Markdown link with a short label in the user's language, never as
the raw address: for example `[打开 EdgePilot 控制台](<url>)`, or, when a strategy target was
opened, `[在 EdgePilot 中查看 <strategy name> <version>](<url>)`.

- A message "Dashboard 已准备好，请点击打开：<url>" is posted by the EdgePilot card's view
  button and already carries a fresh link to that strategy: reply only with that exact URL as
  the link. Do not call `edgepilot_dashboard_open` for it; a new link without the card's target
  would open the Dashboard without the strategy.
- A link signs the browser in once within 10 minutes. When the user asks to open it again
  later, call `edgepilot_dashboard_open` again with the same `target` as before.

## Upgrade recovery

Runtime upgrades are forward-only. Trading runs in the separate local trading service, which
the upgrade drains (instances keep their desired state and resume after the upgrade). Never
edit or delete trading service files, and never recommend repeated repair to clear a
failure; inspect `trading.service.status` and the diagnostics operations instead.

## Failure diagnosis

Use this when the user asks why something failed, pastes an error, a `job_`, `req_` or
`diag_` reference or a run ID, or reports that a strategy stopped or places no orders.
Diagnosis is read-only: never start, stop, retry, repair, cancel orders or close positions
as part of it.

1. Install, update or startup problems, or Host tools unavailable: call
   `edgepilot_runtime_diagnose` (works without a running Runtime).
2. Everything else: search the `diagnostics` toolkit and execute
   `diagnostics.failure.explain` with the reference. Without one, execute
   `diagnostics.failure.list` first and let the user pick if several failures match.
3. Report, in the user's language:
   - **Where it failed**: kind, phase and time from `subject`.
   - **Cause**: `matches` with `owner_classified` confidence are established by the owner;
     `pattern_match` is a likely cause from log text. Quote at most two evidence lines.
     With no match, state `unclassified` and summarize the strongest evidence lines
     yourself, labelled as your inference.
   - **Trading effect**: repeat `effect_guidance`. For `unknown`, tell the user to check
     orders, fills and positions on the exchange before any new action.
   - **What to do** and **how to verify**: the match `actions` and `verify`.
   - **Missing evidence**: translate `gaps` (for example `diagnostic_id_not_written_to_log`
     means the error ID has no log entry yet; `unclassified` means no known signature).
4. When the cause may be the exchange API key (a trading state offers `test_credentials`,
   or the failure is a connection, authentication or starting-capital error), execute
   `credentials.test` with that account's `venue` and `mode`. It only reads the account and
   logs in to the private stream; report its `result`, the failed step's `venue_code` and
   what to change. Never ask the user to paste keys to test them.
5. Never present a guess as the cause, never ask for API keys or tokens, and never tell the
   user to edit or delete EdgePilot state files. If the user wants support, give them the
   reference IDs and the evidence lines, which are already redacted.

## First-use onboarding

Run this flow when the user's entire message is only an EdgePilot Live plugin mention
(apart from whitespace), when the user selects an interactive-onboarding starter prompt,
or when the user explicitly asks to open/start the questionnaire or onboarding. Treat a
mention-only message as a request to launch the Dashboard and interactive onboarding;
do not reply with a generic menu or ask what the user wants to do. A plain request to find
or recommend a strategy is not onboarding. Reply in the user's current language (`en`,
`ko`, `zh-CN` or `zh-TW`); if the mention-only message gives no language signal, use the
conversation's language, then the host locale, and otherwise English.
Ordinary requests such as opening the Dashboard, checking a run or searching the catalog
must go directly to that outcome and must not force the questionnaire.

1. Call `edgepilot_runtime_status`, then `edgepilot_runtime_start` when the bound target
   needs starting, installation or recovery. Let the script decide whether to reuse,
   start, prepare or resume; do not infer process liveness from stored job states or
   historical lifecycle phases and do not assemble alternative shell recovery commands.
   Wait for the original call's final result; yielded/running is not completed. If the
   script reports `runtime_operation_pending`, wait on that call or query status with
   bounded backoff, without parallel open calls or duplicate installations.
   When `state=awaiting_confirmation`, show `switch.jobs` and ask once: “暂不切换”
   (`defer`) or “暂停并继续升级” (`stop_and_continue`), translated into the user's
   language. A `kind: "trading"` entry means running strategies pause during the switch
   (positions and orders stay as they are) and resume automatically once the new version
   is ready. Other entries are trading tasks of the previous version; they are stopped
   keeping their positions, which does not cancel orders or close positions, and the new
   version lists what it finds on the exchange for the user to take over or close.
   Submit the chosen action to the same lifecycle tool with the returned `operation_id`
   and `snapshot_digest`; never invent or reuse a changed snapshot. This choice
   authorizes only the listed pause or stop, not an orders/positions review. A refreshed
   snapshot requires a fresh choice. On `deferred`, end this target
   startup request and leave the old environment alone; do not open old onboarding as
   target success. Report other failures and their script-provided recovery action.
   For `stale_session`, reload the plugin session rather than attempting a downgrade.
2. For this Dashboard-and-onboarding request, all successful paths (already running,
   stopped target started, first installation, upgrade or repair) continue identically.
   Only after `state=ready` and `connection_ready=true`, call
   `edgepilot_dashboard_open` once and present its URL as a link (Dashboard links). Then call
   `edgepilot_onboarding_open` once with the current locale. On success, hand control to
   that interactive card and end the turn. A brief instruction to continue in the card is
   enough; do not repeat questionnaire choices in chat or call another question/selection
   tool. Keep all seven choices, review and recommendation inside that one App.
   The tool does not report rendering visibility. Missing model-visible HTML, missing
   acknowledgement or delayed rendering is unknown, not evidence of failure. Never claim
   the card did not appear based on that absence and never automatically start text onboarding
   alongside a successful App request. Do not restart installation to recover presentation.
3. Switch to text onboarding only when the host explicitly reports App rendering unsupported
   or failed, or the user reports the card unusable or explicitly requests text onboarding.
   A Runtime/tool execution error follows step 1 recovery, not the questionnaire fallback.
   Apply the **one-question turn boundary**
   as the formal fallback. The internal field order is `profit_style`,
   `holding_period`, `pain_point`, `max_drawdown_pct`, `trading_mode`, `allocation_band`,
   `universe`. Ask only the first unanswered field, with only that field's choices, and end
   the assistant turn immediately. Never display the complete questionnaire, a numbered
   checklist, future questions, future choices or a request for multiple answers. Do not
   even preview what comes next.
4. On the user's next message, retain every valid supplied answer and ask only the next
   unanswered field, then end the turn immediately again. If the current answer is invalid
   or ambiguous, clarify only the same field and end the turn; do not advance or expose any
   later field. A message that already contains valid answers may fill them silently, but
   the response still asks at most one unanswered field.
5. After the last answer, use a separate assistant turn to summarize the selected values
   and ask only for explicit confirmation. Do not combine that confirmation request with
   another question and do not call recommendation before confirmation.
6. Only in this explicitly requested textual onboarding fallback, call the hidden App/fallback
   handler `edgepilot_strategy_recommend` once with `questionnaire_version="2.0"`, the seven
   confirmed values and the matching locale. Present exactly the three owner-ranked choices:
   best fit, relatively steadier and more aggressive, preserving versions, evidence,
   trade-offs and warnings.

Onboarding never installs a recommended strategy without selection and never starts
demo or live execution. Authentication remains Dashboard-only and every existing live
confirmation gate remains unchanged.

For strategy work, prefer the Runtime workflow hints. The normal dependency order is
catalog search/recommend, exact inspect, install, configuration resolve, backtest start,
durable job status and result get. Keep the selected slug/version and all returned digests
unchanged. Login is Dashboard-only: ask the user to open the Live Dashboard. Never start
Device Authorization or put credentials, access tokens or refresh tokens in chat.

Trading runs as strategy instances in the local trading service. Demo and live are separate
trading accounts selected by `mode` and `venue`; demo can place orders in an exchange test
account and never implies live.

Deploying is always two-stage. `trading.instance.prepare` validates and freezes the strategy,
configuration and venue for ten minutes. `trading.instance.start` requires the attended
confirmation bound to that exact `prepared_ref`. Never turn a generic “yes” into
authorization and never place credentials in model arguments or prose.

Trading commands (`start`, `stop`, `resume`, `halt`, `exposure.flatten`, `exposure.adopt`)
return when the trading service recorded them; what happened at the exchange is read from
`trading.state.get` (engine, instances, exposure ownership, risk) or `trading.events.list`.
Every state object lists its `actions`; offer only those. Stopping keeps the position by
default (`stop_policy: keep`); flattening closes it and needs the user's explicit choice.
When the outcome of a command is unknown, read the state or repeat with the same
idempotency key; never repeat it with a new key. Orders or positions the platform cannot
attribute block the instance until the user adopts or flattens them from the actions.

After an attended result is presented, stop and wait for the App. App submit invokes the
stored exact Owner call once; do not replay the original Execute as confirmation. Continue
only from a later status/result read.

The local MCP route and bearer are created in an owner-private staged copy by the Runtime
Host. If the connection is unavailable, report that the Runtime/Host must be started
or repaired; do not search for Python, install packages, scan ports or call Marketplace MCP
as an internal substitute.
