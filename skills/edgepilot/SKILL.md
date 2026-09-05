---
name: edgepilot
description: Route strategy discovery, configuration, backtesting, paper, exchange-demo, and attended live execution through the local EdgePilot Runtime Host. Use for EdgePilot Live workflows; never expose credentials or bypass confirmation gates.
---

# EdgePilot Live router

The Node Ready Bridge always exposes `edgepilot_runtime_status`,
`edgepilot_runtime_start`, `edgepilot_runtime_update` and `edgepilot_runtime_repair`, even
before Runtime exists. Call status when Host tools are unavailable, then start once. When
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

For chat recommendation, call the read-only `edgepilot_strategy_recommend` convenience
tool with the user's structured questionnaire; it delegates to
`catalog.strategy.recommend` in the Host. Do not replace recommendation with generic
catalog search. For “open”, “start” or “launch EdgePilot”, ensure Runtime is ready, then
call `edgepilot_dashboard_open`; return its loopback URL and never spawn a legacy Dashboard
directly.

For strategy work, prefer the Runtime workflow hints. The normal dependency order is
catalog search/recommend, exact inspect, install, configuration resolve, backtest start,
durable job status and result get. Keep the selected slug/version and all returned digests
unchanged. Login is Dashboard-only: ask the user to open the Live Dashboard. Never start
Device Authorization or put credentials, access tokens or refresh tokens in chat.

Paper is locally simulated execution. Demo can place orders in an exchange test account.
Both use their explicit `paper.run.*` or `demo.run.*` operations and never imply Live.

Live execution is always two-stage. `live.run.prepare` freezes account, strategy,
configuration, Runtime and risk identity. `live.run.start` requires the attended
confirmation bound to that prepared intent. Never turn a generic “yes” into authorization,
never retry an unknown external effect automatically, and never place credentials in model
arguments or prose.

After an attended result is presented, stop and wait for the App. App submit invokes the
stored exact Owner call once; do not replay the original Execute as confirmation. Continue
only from a later status/result read.

The local MCP route and bearer are created in an owner-private staged copy by the Runtime
Host. If the connection is unavailable, report that the Runtime/Host must be started
or repaired; do not search for Python, install packages, scan ports or call Marketplace MCP
as an internal substitute.
