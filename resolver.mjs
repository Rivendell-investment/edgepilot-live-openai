import { createHash, randomUUID } from "node:crypto";
import { closeSync, constants, existsSync, fstatSync, lstatSync, mkdirSync, openSync, readFileSync, readSync, renameSync, rmSync, writeFileSync } from "node:fs";
import { createInterface } from "node:readline";
import { fileURLToPath } from "node:url";
import { homedir } from "node:os";
import { dirname, isAbsolute, join } from "node:path";
import { spawn, spawnSync } from "node:child_process";

const MAX_MESSAGE_BYTES = 256 * 1024;
const MAX_RESOURCE_BYTES = 768 * 1024;
const MAX_BOOTSTRAP_BYTES = 2 * 1024 * 1024;
const MCP_PROTOCOL_VERSION = "2025-06-18";
const MCP_FORWARD_TIMEOUT_MS = 210_000;
const SEARCH_FORWARD_TIMEOUT_MS = 4_000;
const SUPPORTED_RUNTIME_CONTRACT = Object.freeze({ major: 1, minor: 0 });
const onboardingResourceUri = (runtimeId) => `ui://edgepilot/strategy-onboarding-v1/${runtimeId.slice("sha256:".length)}.html`;
const searchResultsResourceUri = (runtimeId) => `ui://edgepilot/strategy-search-results-v1/${runtimeId.slice("sha256:".length)}.html`;
const HOST_TOOL_NAMES = new Set([
  "edgepilot_connection_list",
  "edgepilot_tool_search",
  "edgepilot_tool_get",
  "edgepilot_tool_execute",
  "edgepilot_result_present",
]);
const profile = process.argv[2];
if (!new Set(["research", "live"]).has(profile)) fatal("invalid_profile");

const root = dirname(fileURLToPath(import.meta.url));
const DISCOVER_INPUT_SCHEMA = readDiscoverSchema();
const DISCOVER_OUTPUT_SCHEMA = readDiscoverOutputSchema();
const pluginVersion = readPluginVersion();
const productVersion = pluginVersion.split("+", 1)[0];
const delivery = readDelivery();
const configuredRoot = process.env.EDGEPILOT_PLUGIN_STATE_ROOT;
if (configuredRoot !== undefined && !isAbsolute(configuredRoot)) fatal("invalid_connection_root");
if (delivery.environment === "local" && configuredRoot === undefined) fatal("local_state_root_required");
const runtimeDirectory = `.edgepilot-runtime-${profile}-production`;
const pluginStateRoot = configuredRoot ?? join(homedir(), runtimeDirectory, "plugins");
const runtimeHome = configuredRoot === undefined ? join(homedir(), runtimeDirectory) : dirname(configuredRoot);
const sharedConnection = join(pluginStateRoot, "connections", `${profile}.json`);
const adjacentConnection = join(root, ".edgepilot-connection.json");
let admittedRuntimeId = null;
let bindingFailure = null;

class BridgeError extends Error {
  constructor(code) { super(code); this.code = code; }
}

async function handleRequest(request) {
  if (request === null || typeof request !== "object" || Array.isArray(request) || request.jsonrpc !== "2.0" || typeof request.method !== "string") {
    return errorResponse(request?.id ?? null, -32600, "invalid_request");
  }
  const notification = !Object.hasOwn(request, "id");
  if (notification) return null;
  const id = request.id;
  if (request.method === "initialize") {
    return resultResponse(id, {
      protocolVersion: MCP_PROTOCOL_VERSION,
      capabilities: { tools: { listChanged: true }, resources: { listChanged: true } },
      serverInfo: {
        name: `edgepilot-ready-bridge-${profile}`,
        version: productVersion,
        ...(readInstalledRuntimeId() === null ? {} : { runtimeId: readInstalledRuntimeId() }),
      },
    });
  }
  if (request.method === "ping") return resultResponse(id, {});
  if (request.method === "tools/list") return resultResponse(id, { tools: await listTools() });
  if (request.method === "tools/call") {
    const name = request.params?.name;
    const argumentsValue = request.params?.arguments ?? {};
    if (typeof name !== "string" || argumentsValue === null || typeof argumentsValue !== "object" || Array.isArray(argumentsValue)) {
      return errorResponse(id, -32602, "invalid_tool_call");
    }
    if (!(name in LIFECYCLE_HANDLERS) && (HOST_TOOL_NAMES.has(name) || ["edgepilot_strategy_search", "edgepilot_strategy_recommend", "edgepilot_onboarding_open", "edgepilot_dashboard_open"].includes(name))) {
      const ready = await ensureForUse();
      if (ready !== null) return resultResponse(id, toolResult(ready, true));
    }
    if (name === "edgepilot_strategy_search") return resultResponse(id, await executeStrategySearch(argumentsValue));
    if (name === "edgepilot_strategy_recommend") return resultResponse(id, await executeHostOperation("catalog.strategy.recommend", recommendationArguments(argumentsValue)));
    if (name === "edgepilot_onboarding_open") {
      const locale = argumentsValue.locale;
      if (!new Set(["en", "ko", "zh-CN", "zh-TW"]).has(locale)) throw new BridgeError("invalid_locale");
      if (await healthyConnection() === null) return resultResponse(id, toolResult({ ...(await runtimeStatus()), message: "runtime_not_ready" }, true));
      return resultResponse(id, {
        ...toolResult({ schema: "edgepilot-strategy-onboarding-v1", profile, locale, questionnaire_version: "2.0" }),
        content: [{ type: "text", text: "Onboarding App requested. Let the user continue in the interactive card and end this turn without repeating questionnaire choices or opening another question tool. Rendering visibility is unknown to this tool; do not claim that the App failed to appear. Use text onboarding only if the host explicitly reports App rendering unsupported/failed, or the user reports the card unusable or explicitly requests text onboarding." }],
      });
    }
    if (name === "edgepilot_dashboard_open") {
      return resultResponse(id, await executeHostOperation("dashboard.open", dashboardArguments(argumentsValue)));
    }
    if (name in LIFECYCLE_HANDLERS) {
      const result = await LIFECYCLE_HANDLERS[name](argumentsValue);
      const response = resultResponse(id, toolResult(result));
      return response;
    }
    if (!HOST_TOOL_NAMES.has(name)) return errorResponse(id, -32601, "tool_not_found");
    return forwardHost(request);
  }
  if (request.method === "resources/list") {
    if ((await runtimeStatus()).state !== "ready") return resultResponse(id, { resources: [] });
    const connection = await healthyConnection();
    if (connection === null) return resultResponse(id, { resources: [] });
    return forward(connection, request);
  }
  if (request.method === "resources/read") {
    const local = readSearchResultsResource(request.params?.uri);
    if (local !== null) return resultResponse(id, { contents: [local] });
    if ((await runtimeStatus()).state !== "ready") return errorResponse(id, -32001, "runtime_not_ready");
    const connection = await healthyConnection();
    if (connection === null) return errorResponse(id, -32001, "runtime_not_ready");
    return forward(connection, request);
  }
  return errorResponse(id, -32601, "method_not_found");
}

const LIFECYCLE_HANDLERS = {
  edgepilot_runtime_blockers: async (argumentsValue) => {
    requireEmpty(argumentsValue);
    if (profile !== "live") throw new BridgeError("tool_not_found");
    return runLifecycle("runtime-blockers");
  },
  edgepilot_runtime_review_job: async (value) => {
    if (profile !== "live" || Object.keys(value).sort().join(",") !== "account_ref,acknowledgement,evidence_digest,job_ref"
        || !/^[0-9a-f]{64}$/.test(value.account_ref) || !/^job_[A-Za-z0-9_-]{20,128}$/.test(value.job_ref)
        || !/^sha256:[0-9a-f]{64}$/.test(value.evidence_digest) || value.acknowledgement !== "orders_and_positions_reviewed") throw new BridgeError("invalid_tool_call");
    return runLifecycle("review-job", { "job-ref": value.job_ref, "account-ref": value.account_ref,
      "evidence-digest": value.evidence_digest, acknowledgement: value.acknowledgement });
  },
  edgepilot_runtime_stop_job: async (value) => {
    if (profile !== "live" || Object.keys(value).sort().join(",") !== "account_ref,idempotency_key,job_ref"
        || !/^[0-9a-f]{64}$/.test(value.account_ref) || !/^job_[A-Za-z0-9_-]{20,128}$/.test(value.job_ref)
        || typeof value.idempotency_key !== "string" || !/^[\x20-\x7e]{16,128}$/.test(value.idempotency_key)) throw new BridgeError("invalid_tool_call");
    return runLifecycle("stop-job", { "job-ref": value.job_ref, "account-ref": value.account_ref, "idempotency-key": value.idempotency_key });
  },
  edgepilot_runtime_status: async (argumentsValue) => {
    requireEmpty(argumentsValue);
    return await runtimeStatus();
  },
  edgepilot_runtime_start: async (argumentsValue) => {
    return runLifecycle("ensure-start", switchArguments(argumentsValue));
  },
  edgepilot_runtime_update: async (argumentsValue) => {
    return runLifecycle("update", switchArguments(argumentsValue));
  },
  edgepilot_runtime_repair: async (argumentsValue) => {
    return runLifecycle("repair", switchArguments(argumentsValue));
  },
};

async function listTools() {
  const connection = await healthyConnection();
  const lifecycle = lifecycleTools(connection?.runtime_id ?? coldOnboardingRuntimeId());
  if (connection === null) return lifecycle;
  const response = await forward(connection, { jsonrpc: "2.0", id: "bridge-tools", method: "tools/list", params: {} });
  const tools = response?.result?.tools;
  return Array.isArray(tools) ? [...lifecycle, ...tools] : lifecycle;
}

// Pin discovery to release metadata before installation, without starting the Runtime.
// Never choose an arbitrary platform from a multi-Runtime release binding.
function coldOnboardingRuntimeId() {
  if (bindingFailure !== null) return null;
  const installedId = readInstalledRuntimeId();
  if (installedId !== null) {
    const runtime = readInstalledRuntime(installedId);
    if (runtime === null || !supportsRuntimeContract(runtime.contractVersion)
        || compareProductVersions(runtime.releaseVersion, productVersion) > 0
        || (admittedRuntimeId !== null && admittedRuntimeId !== installedId)) return null;
    // An older installation still needs the target URI in the client's first tool list.
    // This advertises presentation only; resource reads retain healthyConnection admission.
    if (delivery.expected_runtime_ids.length === 1) return delivery.expected_runtime_ids[0];
    return matchesRelease(installedId, runtime) ? installedId : null;
  }
  return delivery.expected_runtime_ids.length === 1 ? delivery.expected_runtime_ids[0] : null;
}

function lifecycleTools(runtimeId = null) {
  const emptyInput = { type: "object", properties: {}, required: [], additionalProperties: false };
  const switchInput = { type: "object", oneOf: [emptyInput, { type: "object", additionalProperties: false,
    properties: { action: { enum: ["defer", "stop_and_continue"] }, operation_id: { type: "string", pattern: "^[0-9a-f-]{36}$" }, snapshot_digest: { type: "string", pattern: "^sha256:[0-9a-f]{64}$" } },
    required: ["action", "operation_id", "snapshot_digest"] }] };
  const output = {
    type: "object",
    properties: {
      state: { enum: ["ready", "stopped", "not_installed", "update_required", "awaiting_confirmation", "deferred", "stale_session", "error"] },
      switch: { type: ["object", "null"] },
      profile: { enum: ["live", "research"] },
      plugin_version: { type: "string" },
      version: { type: "string" },
      expected_product_version: { type: "string" },
      runtime_version: { type: ["string", "null"] },
      runtime_contract_version: { type: ["string", "null"] },
      runtime_id: { type: ["string", "null"] },
      bootstrap_installed: { type: "boolean" },
      lifecycle: { type: ["object", "null"] },
      connection_ready: { type: "boolean" },
      repair_allowed: { type: "boolean" },
      required_action: { type: ["string", "null"] },
      message: { type: ["string", "null"] },
    },
    required: ["state", "profile", "version", "plugin_version", "expected_product_version", "runtime_version", "runtime_contract_version", "runtime_id", "bootstrap_installed", "connection_ready", "repair_allowed", "required_action", "message"],
    additionalProperties: false,
  };
  const definitions = [
    ["edgepilot_runtime_status", "Runtime Status", "Inspect local EdgePilot Runtime and Host readiness without starting them.", true],
    ["edgepilot_runtime_start", "Runtime Start", "Download/install when needed and start the local EdgePilot Runtime Host.", false],
    ["edgepilot_runtime_update", "Runtime Update", "Update to the channel Runtime and restart the local Host.", false],
    ["edgepilot_runtime_repair", "Runtime Repair", "Reinstall the channel Runtime and restart the local Host.", false],
  ];
  const tools = definitions.map(([name, title, description, readOnly]) => ({
    name, title, description: readOnly ? description : `${description} If a prepared replacement needs old processes stopped, returns awaiting_confirmation with their exact snapshot. Only submit stop_and_continue after the user chooses to stop those listed processes; defer leaves the old installation running.`, inputSchema: readOnly ? emptyInput : switchInput, outputSchema: output,
    annotations: { readOnlyHint: readOnly, destructiveHint: !readOnly, idempotentHint: true, openWorldHint: !readOnly },
  }));
  if (profile === "live") {
    tools.push({ name: "edgepilot_runtime_blockers", title: "Inspect Runtime Blockers", description: "Inspect old Live jobs even when plugin and Runtime versions differ. May prepare the fixed Runtime maintenance executable; never starts trading.", inputSchema: emptyInput,
      annotations: { readOnlyHint: false, destructiveHint: false, idempotentHint: true, openWorldHint: true } });
    tools.push({ name: "edgepilot_runtime_review_job", title: "Record Operator Review", description: "Only after the user explicitly confirms reviewing the identified stopped job's outstanding orders and positions, record that review against its current evidence digest. Keeps the unknown outcome; never starts trading or changes exchange state.",
      inputSchema: { type: "object", additionalProperties: false, required: ["job_ref", "account_ref", "evidence_digest", "acknowledgement"], properties: {
        job_ref: { type: "string", pattern: "^job_[A-Za-z0-9_-]{20,128}$" }, account_ref: { type: "string", pattern: "^[0-9a-f]{64}$" }, evidence_digest: { type: "string", pattern: "^sha256:[0-9a-f]{64}$" }, acknowledgement: { const: "orders_and_positions_reviewed" } } },
      annotations: { readOnlyHint: false, destructiveHint: true, idempotentHint: true, openWorldHint: false } });
    tools.push({ name: "edgepilot_runtime_stop_job", title: "Stop Runtime Job", description: "Stop one explicitly selected Live job using its exact account and job identity. Requires the user's request to stop that job; does not imply order cancellation or position closure.",
      inputSchema: { type: "object", additionalProperties: false, required: ["job_ref", "account_ref", "idempotency_key"], properties: {
        job_ref: { type: "string", pattern: "^job_[A-Za-z0-9_-]{20,128}$" }, account_ref: { type: "string", pattern: "^[0-9a-f]{64}$" }, idempotency_key: { type: "string", minLength: 16, maxLength: 128 } } },
      annotations: { readOnlyHint: false, destructiveHint: true, idempotentHint: true, openWorldHint: false } });
  }
  tools.push({
    name: "edgepilot_strategy_search", title: "Search Strategies",
    description: `Search profile-scoped strategies for ordinary chat requests with multilingual relevance, strict filters, facets, and match explanations. When the user asks for an exact number of recommendations, pass that number as limit (for example 1, 2, or 3) so the conversation and App show the same count. Use a larger limit only when the user asks for options or multiple candidates. ${profile === "live" ? "For a Live plugin mention with no other message content, open Dashboard and onboarding; do not search." : "Open onboarding only when the user explicitly asks for the questionnaire."}`,
    inputSchema: searchSchema(),
    outputSchema: searchOutputSchema(),
    annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: true },
    ...(runtimeId === null ? {} : {
      _meta: {
        ui: { resourceUri: searchResultsResourceUri(runtimeId) },
        "openai/outputTemplate": searchResultsResourceUri(runtimeId),
      },
    }),
  });
  tools.push({
    name: "edgepilot_onboarding_open", title: "Open Strategy Onboarding",
    description: `Open the interactive seven-question strategy onboarding and show its owner-computed recommendation in the same App.${profile === "live" ? " A Live plugin mention with no other message content requests this onboarding after Dashboard opens." : ""}`,
    inputSchema: { type: "object", properties: { locale: { enum: ["en", "ko", "zh-CN", "zh-TW"] } }, required: ["locale"], additionalProperties: false },
    outputSchema: { type: "object", properties: { schema: { const: "edgepilot-strategy-onboarding-v1" }, profile: { enum: ["live", "research"] }, locale: { enum: ["en", "ko", "zh-CN", "zh-TW"] }, questionnaire_version: { const: "2.0" } }, required: ["schema", "profile", "locale", "questionnaire_version"], additionalProperties: false },
    annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: false },
    ...(runtimeId === null ? {} : {
      _meta: {
        ui: { resourceUri: onboardingResourceUri(runtimeId) },
        "openai/outputTemplate": onboardingResourceUri(runtimeId),
      },
    }),
  });
  tools.push({
    name: "edgepilot_dashboard_open", title: "Open Dashboard",
    description: "Start or reuse the Host-owned profile Dashboard and optionally open one exact typed target. A target only controls navigation; it never installs or runs anything.",
    inputSchema: dashboardSchema(),
    annotations: { readOnlyHint: false, destructiveHint: false, idempotentHint: true, openWorldHint: false },
  });
  return tools;
}

function recommendationSchema() {
  const locale = { enum: ["en", "ko", "zh-CN", "zh-TW"] };
  return { type: "object", additionalProperties: false, properties: {
    questionnaire_version: { const: "2.0" },
    profit_style: { enum: ["trend", "reversal", "relative_value"] },
    holding_period: { enum: ["intraday", "multi_day", "multi_week"] },
    pain_point: { enum: ["loss_streak", "inactivity", "tail_loss"] },
    max_drawdown_pct: { enum: [5, 15, 20] },
    trading_mode: { enum: ["long_only_no_leverage", "long_short_low_leverage", "long_short_high_leverage"] },
    allocation_band: { enum: ["under_25k", "25k_100k", "over_100k"] },
    universe: { enum: ["majors", "altcoins", "any"] },
    locale,
  }, required: ["questionnaire_version", "profit_style", "holding_period", "pain_point", "max_drawdown_pct", "trading_mode", "allocation_band", "universe", "locale"] };
}

function dashboardTargetSchema() {
  return {
    type: "object",
    additionalProperties: false,
    properties: {
      kind: { const: "strategy" },
      slug: { type: "string", pattern: "^[a-z0-9]+(?:-[a-z0-9]+)*$", maxLength: 80 },
      version: { type: "string", pattern: "^(0|[1-9]\\d*)\\.(0|[1-9]\\d*)\\.(0|[1-9]\\d*)(?:-((?:0|[1-9]\\d*|\\d*[A-Za-z-][0-9A-Za-z-]*)(?:\\.(?:0|[1-9]\\d*|\\d*[A-Za-z-][0-9A-Za-z-]*))*))?(?:\\+([0-9A-Za-z-]+(?:\\.[0-9A-Za-z-]+)*))?$", maxLength: 64 },
      content_sha256: { type: "string", pattern: "^[0-9a-f]{64}$" },
    },
    required: ["kind", "slug", "version", "content_sha256"],
  };
}

function dashboardSchema() {
  return {
    type: "object",
    additionalProperties: false,
    properties: { target: dashboardTargetSchema() },
    required: [],
  };
}

function dashboardArguments(value) {
  if (Object.keys(value).length === 0) return {};
  if (Object.keys(value).sort().join(",") !== "target" || value.target === null
      || typeof value.target !== "object" || Array.isArray(value.target)) throw new BridgeError("invalid_tool_call");
  const target = value.target;
  if (Object.keys(target).sort().join(",") !== "content_sha256,kind,slug,version"
      || target.kind !== "strategy" || typeof target.slug !== "string"
      || !/^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(target.slug) || target.slug.length > 80
      || typeof target.version !== "string" || target.version.length > 64
      || !/^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-((?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)(?:\.(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*))*))?(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$/.test(target.version)
      || typeof target.content_sha256 !== "string" || !/^[0-9a-f]{64}$/.test(target.content_sha256)) throw new BridgeError("invalid_tool_call");
  return { target: { ...target } };
}

function recommendationArguments(value) {
  const schema = recommendationSchema();
  if (Object.keys(value).sort().join(",") !== [...schema.required].sort().join(",")) throw new BridgeError("invalid_tool_call");
  for (const [key, property] of Object.entries(schema.properties)) {
    if (Object.hasOwn(property, "const") && value[key] !== property.const) throw new BridgeError("invalid_tool_call");
    if (Array.isArray(property.enum) && !property.enum.includes(value[key])) throw new BridgeError("invalid_tool_call");
  }
  return { ...value };
}

function searchSchema() {
  return DISCOVER_INPUT_SCHEMA;
}

function searchOutputSchema() {
  return {
    type: "object",
    additionalProperties: false,
    properties: {
      schema: { const: "edgepilot-strategy-search-results-v1" },
      profile: { enum: ["live", "research"] },
      request: DISCOVER_INPUT_SCHEMA,
      result: DISCOVER_OUTPUT_SCHEMA,
    },
    required: ["schema", "profile", "request", "result"],
  };
}

function searchArguments(value) {
  const allowed = new Set(Object.keys(searchSchema().properties));
  if (Object.keys(value).some(key => !allowed.has(key))) throw new BridgeError("invalid_tool_call");
  return { ...value, diversity: value.diversity ?? "none", limit: value.limit ?? 10 };
}

function readDiscoverSchema() {
  let value;
  try { value = JSON.parse(readFileSync(join(root, "strategy-discover-input.json"), "utf8")); }
  catch { fatal("discover_contract_missing"); }
  if (value?.type !== "object" || value.additionalProperties !== false || !Array.isArray(value.required)
      || !value.required.includes("locale") || !value.required.includes("limit") || !value.required.includes("diversity")) {
    fatal("discover_contract_invalid");
  }
  return Object.freeze(value);
}

function readDiscoverOutputSchema() {
  let value;
  try { value = JSON.parse(readFileSync(join(root, "strategy-discover-output.json"), "utf8")); }
  catch { fatal("discover_contract_missing"); }
  if (value?.type !== "object" || value.additionalProperties !== false || !Array.isArray(value.required)
      || !value.required.includes("strategies") || value.properties?.contract_version?.const !== "2.1") {
    fatal("discover_contract_invalid");
  }
  return Object.freeze(value);
}

async function executeStrategySearch(value) {
  const request = searchArguments(value);
  const result = await executeHostOperation("catalog.strategy.discover", request, SEARCH_FORWARD_TIMEOUT_MS);
  if (result?.isError === true) return result;
  const outcomes = result?.structuredContent?.outcomes;
  const outcome = Array.isArray(outcomes) && outcomes.length === 1 ? outcomes[0] : null;
  const output = outcome?.output;
  if (outcome?.operation_id !== "catalog.strategy.discover" || outcome?.status !== "completed"
      || output === null || typeof output !== "object" || Array.isArray(output)
      || output.contract_version !== "2.1" || !Array.isArray(output.strategies)) {
    return toolResult({ schema: "edgepilot-strategy-search-failure-v1", profile, code: "search_result_invalid" }, true);
  }
  const payload = { schema: "edgepilot-strategy-search-results-v1", profile, request, result: output };
  return {
    content: [{ type: "text", text: searchFallback(payload) }],
    structuredContent: payload,
  };
}

function searchFallback(payload) {
  const strategies = Number.isInteger(payload.request.limit)
    ? payload.result.strategies.slice(0, payload.request.limit)
    : payload.result.strategies;
  const count = strategies.length;
  const prefix = payload.request.locale === "zh-CN" ? `已准备 ${count} 个搜索结果`
    : payload.request.locale === "zh-TW" ? `已準備 ${count} 個搜尋結果`
      : payload.request.locale === "ko" ? `${count}개의 검색 결과가 준비되었습니다`
        : `${count} search results are ready`;
  const identities = strategies
    .map(strategy => `${strategy.name} (${strategy.slug}@${strategy.version})`)
    .join(", ");
  const notes = [...(payload.result.needs_clarification ?? []), ...(payload.result.unsupported_constraints ?? []), ...(payload.result.relaxed_filters ?? [])];
  return `${prefix}${identities ? `: ${identities}` : ""}${notes.length ? `. Notes: ${notes.join("; ")}` : ""}. Use the interactive strategy result cards to open a strategy in Dashboard.`;
}

// 只读搜索卡必须绑定请求中的精确 Runtime release，但不应被 Host 会话准入状态阻断。
function readSearchResultsResource(uri) {
  if (typeof uri !== "string") return null;
  const match = /^ui:\/\/edgepilot\/strategy-search-results-v1\/([0-9a-f]{64})\.html$/.exec(uri);
  if (match === null) return null;
  const runtimeId = `sha256:${match[1]}`;
  const runtimeRoot = join(runtimeHome, "runtime");
  const releasesRoot = join(runtimeRoot, "releases");
  const release = join(runtimeHome, "runtime", "releases", match[1]);
  const appsRoot = join(release, "apps");
  const appRoot = join(release, "apps", "mcp-app");
  const path = join(appRoot, "search-results.html");
  try {
    const runtimeMetadata = lstatSync(runtimeRoot);
    const releasesMetadata = lstatSync(releasesRoot);
    const releaseMetadata = lstatSync(release);
    const appsMetadata = lstatSync(appsRoot);
    const appMetadata = lstatSync(appRoot);
    const manifestPath = join(release, "RUNTIME.json");
    const manifestMetadata = lstatSync(manifestPath);
    const metadata = lstatSync(path);
    if (!runtimeMetadata.isDirectory() || runtimeMetadata.isSymbolicLink()
        || !releasesMetadata.isDirectory() || releasesMetadata.isSymbolicLink()
        || !releaseMetadata.isDirectory() || releaseMetadata.isSymbolicLink()
        || !appsMetadata.isDirectory() || appsMetadata.isSymbolicLink()
        || !appMetadata.isDirectory() || appMetadata.isSymbolicLink()
        || !manifestMetadata.isFile() || manifestMetadata.isSymbolicLink() || manifestMetadata.size > 256 * 1024
        || !metadata.isFile() || metadata.isSymbolicLink() || metadata.size > MAX_RESOURCE_BYTES) return null;
    const manifestBytes = readBoundedRegularFile(manifestPath, 256 * 1024);
    if (manifestBytes === null) return null;
    const manifest = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(manifestBytes));
    const runtime = {
      releaseVersion: manifest?.payload?.release_version,
      contractVersion: manifest?.payload?.contract_version,
    };
    if (manifest?.runtime_id !== runtimeId || !Array.isArray(manifest?.payload?.profiles)
        || !manifest.payload.profiles.includes(profile) || runtimePayloadDigest(manifest.payload) !== runtimeId
        || typeof runtime.releaseVersion !== "string"
        || !Number.isInteger(runtime.contractVersion?.major) || !Number.isInteger(runtime.contractVersion?.minor)
        || !supportsRuntimeContract(runtime.contractVersion) || !matchesRelease(runtimeId, runtime)) return null;
    const entries = manifest.payload.files;
    const expected = Array.isArray(entries)
      ? entries.filter(entry => entry?.path === "apps/mcp-app/search-results.html")
      : [];
    if (expected.length !== 1 || expected[0].kind !== "file" || expected[0].executable !== false
        || expected[0].size !== metadata.size || !/^sha256:[0-9a-f]{64}$/.test(expected[0].sha256)) return null;
    const bytes = readBoundedRegularFile(path, MAX_RESOURCE_BYTES);
    if (bytes === null || bytes.length !== metadata.size) return null;
    if (`sha256:${createHash("sha256").update(bytes).digest("hex")}` !== expected[0].sha256) return null;
    const port = process.env[profile === "live" ? "EDGEPILOT_LIVE_DASHBOARD_PORT" : "EDGEPILOT_RESEARCH_DASHBOARD_PORT"]
      ?? (profile === "live" ? "8787" : "8686");
    if (!/^[1-9][0-9]{0,4}$/.test(port) || Number(port) > 65535) return null;
    const origin = `http://127.0.0.1:${port}`;
    return {
      uri,
      mimeType: "text/html;profile=mcp-app",
      text: new TextDecoder("utf-8", { fatal: true }).decode(bytes),
      _meta: {
        ui: {
          prefersBorder: true,
          csp: { connectDomains: [], resourceDomains: [] },
        },
        "openai/widgetPrefersBorder": true,
        "openai/widgetCSP": {
          connect_domains: [],
          resource_domains: [],
          redirect_domains: [origin],
        },
      },
    };
  } catch { return null; }
}

function readBoundedRegularFile(path, maximumBytes) {
  let descriptor;
  try {
    descriptor = openSync(path, constants.O_RDONLY | (constants.O_NOFOLLOW ?? 0));
    const metadata = fstatSync(descriptor);
    if (!metadata.isFile() || metadata.size > maximumBytes) return null;
    const bytes = Buffer.alloc(metadata.size);
    let offset = 0;
    while (offset < bytes.length) {
      const count = readSync(descriptor, bytes, offset, bytes.length - offset, offset);
      if (count === 0) return null;
      offset += count;
    }
    return bytes;
  } catch {
    return null;
  } finally {
    if (descriptor !== undefined) closeSync(descriptor);
  }
}

function runtimePayloadDigest(payload) {
  const canonical = value => {
    if (value === null || typeof value === "string" || typeof value === "boolean") return JSON.stringify(value);
    if (typeof value === "number" && Number.isFinite(value)) return JSON.stringify(value);
    if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
    if (value !== null && typeof value === "object" && Object.getPrototypeOf(value) === Object.prototype) {
      return `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${canonical(value[key])}`).join(",")}}`;
    }
    throw new TypeError("runtime manifest payload is not canonical JSON");
  };
  return `sha256:${createHash("sha256").update(canonical(payload)).digest("hex")}`;
}

async function executeHostOperation(operationId, argumentsValue, timeoutMs = MCP_FORWARD_TIMEOUT_MS) {
  const connection = await healthyConnection();
  if (connection === null) return toolResult({ ...(await runtimeStatus()), message: "runtime_not_ready" }, true);
  const get = await forward(connection, { jsonrpc: "2.0", id: "bridge-operation-get", method: "tools/call", params: { name: "edgepilot_tool_get", arguments: { operation_ids: [operationId], include_output_schema: false } } }, timeoutMs);
  const operation = get?.result?.structuredContent?.operations?.[0];
  if (operation?.id !== operationId || typeof operation.schema_revision !== "string") throw new BridgeError("operation_unavailable");
  const execute = await forward(connection, { jsonrpc: "2.0", id: "bridge-operation-execute", method: "tools/call", params: { name: "edgepilot_tool_execute", arguments: { calls: [{ call_id: `bridge-${operationId.replaceAll(".", "-")}`, operation_id: operationId, schema_revision: operation.schema_revision, authority: "direct", arguments: argumentsValue }], presentation: "never" } } }, timeoutMs);
  if (execute?.error) throw new BridgeError(String(execute.error.message ?? "operation_failed"));
  return execute.result;
}

async function runLifecycle(command, managementArguments = {}) {
  const management = ["runtime-blockers", "stop-job", "review-job"].includes(command);
  const before = await runtimeStatus();
  if (!management && before.state === "stale_session") return before;
  if (!management && before.message === "runtime_operation_pending") return before;
  if (new Set(["update", "repair"]).has(command) && !before.repair_allowed) return staleSession(before);
  const channelUrl = process.env.EDGEPILOT_CHANNEL_URL ?? delivery.channel_url;
  const bootstrap = validateBootstrapFile(configuredBootstrapPath());
  const args = [bootstrap, command, "--runtime-home", runtimeHome, "--channel-url", channelUrl, "--product", profile, "--plugin-version", pluginVersion];
  args.push("--expected-product-version", productVersion);
  for (const runtimeId of delivery.expected_runtime_ids) args.push("--expected-runtime-id", runtimeId);
  args.push("--environment", delivery.environment);
  if (delivery.marketplace_origin !== null) args.push("--marketplace-origin", delivery.marketplace_origin);
  if (process.env.EDGEPILOT_LIVE_STATE_ROOT) args.push("--live-state-root", process.env.EDGEPILOT_LIVE_STATE_ROOT);
  if (process.env.EDGEPILOT_RESEARCH_STATE_ROOT) args.push("--research-state-root", process.env.EDGEPILOT_RESEARCH_STATE_ROOT);
  for (const [key, value] of Object.entries(managementArguments)) args.push(`--${key}`, value);
  const completed = await runLifecycleProcess(args);
  if (completed.error?.code === "ETIMEDOUT") throw new BridgeError("runtime_timeout");
  if (completed.status !== 0) {
    const code = /^EdgePilot bootstrap: ([a-z0-9_]+)$/mu.exec(completed.stderr ?? "")?.[1] ?? "bootstrap_failed";
    if (new Set(["runtime_identity_incompatible", "runtime_version_incompatible"]).has(code)) {
      bindingFailure = code;
      return runtimeStatus();
    }
    if (new Set(["plugin_incompatible", "contract_incompatible"]).has(code)) return staleSession(await runtimeStatus());
    return { ...(await runtimeStatus()), state: "error", connection_ready: false, message: code,
      required_action: code === "runtime_pinned" ? "inspect_runtime_blockers" : code.startsWith("dashboard_") ? "inspect_startup_diagnostics" : "inspect_runtime_status" };
  }
  if (management) {
    try { return JSON.parse(completed.stdout); } catch { throw new BridgeError("maintenance_response_invalid"); }
  }
  let outcome;
  try { outcome = JSON.parse(completed.stdout); } catch { /* Older lifecycle fixtures return no structured output. */ }
  if (["awaiting_confirmation", "deferred"].includes(outcome?.state)) {
    return { ...(await runtimeStatus()), state: outcome.state, switch: outcome.switch, lifecycle: outcome.lifecycle,
      connection_ready: false, required_action: outcome.required_action, message: outcome.state === "awaiting_confirmation" ? "runtime_switch_confirmation_required" : "runtime_switch_deferred" };
  }
  const connection = await healthyConnection();
  if (connection === null) return { ...(await runtimeStatus()), state: "error", message: "host_not_ready" };
  admittedRuntimeId = connection.runtime_id;
  const result = await runtimeStatus();
  if (result.state !== "ready") return { ...result, message: result.message ?? "host_not_ready" };
  setTimeout(() => {
    writeResponse({ jsonrpc: "2.0", method: "notifications/tools/list_changed" });
    writeResponse({ jsonrpc: "2.0", method: "notifications/resources/list_changed" });
  }, 0);
  return result;
}

async function ensureForUse() {
  const status = await runtimeStatus();
  if (status.state === "stale_session") return status;
  if (status.message === "runtime_operation_pending" || status.message === "runtime_operation_identity_unverified") return status;
  if (["awaiting_confirmation", "deferred"].includes(status.state)) return status;
  if (status.state === "ready") { admittedRuntimeId = status.runtime_id; return null; }
  const result = await runLifecycle("ensure-start");
  return result.state === "ready" ? null : result;
}

function lifecycleSnapshot() {
  const path = join(runtimeHome, "runtime", "lifecycle.json");
  if (!existsSync(path)) return null;
  try {
    const metadata = lstatSync(path);
    if (!metadata.isFile() || metadata.isSymbolicLink() || metadata.size > 65536) return { phase: "repair_required", last_error: "lifecycle_state_invalid" };
    const value = JSON.parse(readFileSync(path, "utf8"));
    if (value === null || typeof value !== "object" || Array.isArray(value)
        || !["prepare", "inspect", "awaiting_confirmation", "deferred", "quiesce", "retire", "migrate", "start", "commit", "ready", "blocked", "repair_required"].includes(value.phase)) {
      return { phase: "repair_required", last_error: "lifecycle_state_invalid" };
    }
    return Object.fromEntries(["operation_id", "target_version", "target_runtime_id", "phase", "last_error", "updated_at", "cleanup_pending", "selection", "blockers"].map(key => [key, value[key] ?? null]));
  } catch { return { phase: "repair_required", last_error: "lifecycle_state_invalid" }; }
}

function lifecycleExecution() {
  const lock = join(runtimeHome, "runtime", "lifecycle.lock"), path = join(lock, "owner.json");
  if (!existsSync(lock)) return null;
  try {
    if (!lstatSync(lock).isDirectory() || lstatSync(lock).isSymbolicLink()
        || !lstatSync(path).isFile() || lstatSync(path).isSymbolicLink() || lstatSync(path).size > 4096) return "unverified";
    const owner = JSON.parse(readFileSync(path, "utf8"));
    if (!Number.isSafeInteger(owner.pid) || owner.pid <= 0) return "unverified";
    try { process.kill(owner.pid, 0); } catch (error) { return error.code === "ESRCH" ? null : "unverified"; }
    let birth = null;
    if (process.platform === "linux") {
      birth = `linux:${readFileSync(`/proc/${owner.pid}/stat`, "utf8").split(") ")[1].split(" ")[19]}`;
    } else if (process.platform === "darwin") {
      const result = spawnSync("/bin/ps", ["-o", "lstart=", "-p", String(owner.pid)], { encoding: "utf8", timeout: 1000 });
      if (result.status === 0 && result.stdout.trim()) birth = `macos:${result.stdout.trim()}`;
    } else if (process.platform === "win32") {
      const powershell = join(process.env.SYSTEMROOT ?? "C:\\Windows", "System32", "WindowsPowerShell", "v1.0", "powershell.exe");
      const result = spawnSync(powershell, ["-NoProfile", "-NonInteractive", "-Command", `(Get-Process -Id ${owner.pid} -ErrorAction Stop).StartTime.ToUniversalTime().Ticks`], { encoding: "utf8", timeout: 3000, windowsHide: true });
      if (result.status === 0 && /^\d+$/.test(result.stdout.trim())) birth = `windows:${result.stdout.trim()}`;
    }
    return birth === null || typeof owner.birth !== "string" ? "unverified" : birth === owner.birth ? "running" : null;
  } catch { return existsSync(lock) ? "unverified" : null; }
}

async function runtimeStatus() {
  const runtimeId = readInstalledRuntimeId();
  const bootstrap = configuredBootstrapPath();
  const runtime = readInstalledRuntime(runtimeId);
  const incompatible = runtime !== null && !supportsRuntimeContract(runtime.contractVersion);
  const newerThanPlugin = runtime !== null && compareProductVersions(runtime.releaseVersion, productVersion) > 0;
  const executing = lifecycleExecution();
  const connection = incompatible || executing !== null ? null : await healthyConnection(runtimeId, runtime);
  const base = {
    profile,
    lifecycle: lifecycleSnapshot(),
    version: productVersion,
    plugin_version: pluginVersion,
    expected_product_version: delivery.expected_product_version,
    runtime_version: runtime?.releaseVersion ?? null,
    runtime_contract_version: runtimeContractLabel(runtime?.contractVersion ?? null),
    runtime_id: connection?.runtime_id ?? runtimeId,
    bootstrap_installed: existsSync(bootstrap),
    connection_ready: connection !== null,
  };
  if (bindingFailure !== null) return { ...base, state: "stale_session", connection_ready: false,
    repair_allowed: false, required_action: "update_plugin_and_reload", message: bindingFailure };
  if (incompatible || newerThanPlugin || (admittedRuntimeId !== null && runtimeId !== admittedRuntimeId)) return staleSession(base);
  if (executing !== null) return { ...base, state: executing === "running" ? "stopped" : "error", connection_ready: false,
    repair_allowed: false, required_action: executing === "running" ? "wait_for_runtime_status" : "inspect_runtime_status",
    message: executing === "running" ? "runtime_operation_pending" : "runtime_operation_identity_unverified" };
  if (base.lifecycle?.selection && base.lifecycle.target_version === productVersion
      && delivery.expected_runtime_ids.includes(base.lifecycle.selection.target_runtime_id)
      && ["awaiting_confirmation", "deferred"].includes(base.lifecycle.phase)) {
    return { ...base, state: base.lifecycle.phase, switch: base.lifecycle.selection, connection_ready: false,
      repair_allowed: true, required_action: base.lifecycle.phase === "awaiting_confirmation" ? "choose_runtime_switch" : null,
      message: base.lifecycle.phase === "awaiting_confirmation" ? "runtime_switch_confirmation_required" : "runtime_switch_deferred" };
  }
  if (runtime !== null && !matchesRelease(runtimeId, runtime)) {
    return { ...base, state: "update_required", connection_ready: false, repair_allowed: true, required_action: "start", message: "runtime_update_required" };
  }
  return {
    ...base,
    state: connection === null ? (runtimeId === null ? "not_installed" : "stopped") : "ready",
    repair_allowed: !newerThanPlugin,
    required_action: null,
    message: runtimeId !== null && runtime === null ? "runtime_manifest_invalid" : null,
  };
}

function staleSession(value) {
  return { ...value, state: "stale_session", connection_ready: false, repair_allowed: false, required_action: "reload_or_new_task", message: "plugin_session_stale" };
}

function readInstalledRuntimeId() {
  try {
    const value = JSON.parse(readFileSync(join(runtimeHome, "runtime", "current.json"), "utf8"));
    return typeof value.current_runtime_id === "string" ? value.current_runtime_id : null;
  } catch { return null; }
}

function readInstalledRuntime(runtimeId) {
  if (runtimeId === null || !/^sha256:[0-9a-f]{64}$/.test(runtimeId)) return null;
  try {
    const manifest = JSON.parse(readFileSync(join(runtimeHome, "runtime", "releases", runtimeId.slice(7), "RUNTIME.json"), "utf8"));
    const releaseVersion = manifest?.payload?.release_version;
    const contractVersion = manifest?.payload?.contract_version;
    if (typeof releaseVersion !== "string" || !Number.isInteger(contractVersion?.major) || !Number.isInteger(contractVersion?.minor)) return null;
    return { releaseVersion, contractVersion };
  } catch { return null; }
}

function supportsRuntimeContract(value) {
  return value?.major === SUPPORTED_RUNTIME_CONTRACT.major && value.minor <= SUPPORTED_RUNTIME_CONTRACT.minor;
}

function runtimeContractLabel(value) {
  return value === null ? null : `${value.major}.${value.minor}`;
}

function compareProductVersions(left, right) {
  const parse = (value) => /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)/.exec(value)?.slice(1).map(Number) ?? null;
  const a = parse(left);
  const b = parse(right);
  if (a === null || b === null) return 0;
  for (let index = 0; index < 3; index += 1) if (a[index] !== b[index]) return a[index] < b[index] ? -1 : 1;
  return 0;
}

async function forwardHost(request) {
  const connection = await healthyConnection();
  if (connection === null) {
    return resultResponse(request.id, toolResult({ ...(await runtimeStatus()), message: "runtime_not_ready" }, true));
  }
  return forward(connection, request);
}

async function forward(connection, request, timeoutMs = MCP_FORWARD_TIMEOUT_MS) {
  const controller = new AbortController();
  const deadline = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(connection.endpoint, {
      method: "POST",
      headers: {
        "Authorization": ["Bearer", connection.bearer_token].join(" "),
        "Content-Type": "application/json",
        "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
        "X-EdgePilot-Runtime-ID": connection.runtime_id,
      },
      body: JSON.stringify(request),
      signal: controller.signal,
      redirect: "error",
    });
    if (response.status === 202) return null;
    const body = await response.text();
    const maximum = request.method === "resources/read" ? MAX_RESOURCE_BYTES : MAX_MESSAGE_BYTES;
    if (Buffer.byteLength(body, "utf8") > maximum) throw new BridgeError("response_too_large");
    if (!response.ok) throw new BridgeError(`host_http_${response.status}`);
    return JSON.parse(body);
  } catch (error) {
    if (error?.name === "AbortError") throw new BridgeError("host_timeout");
    if (error instanceof BridgeError) throw error;
    throw new BridgeError("host_unavailable");
  } finally {
    clearTimeout(deadline);
  }
}

function readConnection() {
  const path = existsSync(adjacentConnection) ? adjacentConnection : sharedConnection;
  let metadata;
  let connection;
  try {
    metadata = lstatSync(path);
    connection = JSON.parse(readFileSync(path, "utf8"));
  } catch { return null; }
  if (!metadata.isFile() || metadata.isSymbolicLink() || metadata.size > 4096) return null;
  if (process.platform !== "win32" && (metadata.mode & 0o077) !== 0) return null;
  if (Object.keys(connection).sort().join(",") !== "bearer_token,endpoint,profile,runtime_id,schema"
      || connection.schema !== "edgepilot-loopback-connection-v1" || connection.profile !== profile
      || !/^[A-Za-z0-9._~-]{40,512}$/.test(connection.bearer_token)) return null;
  let endpoint;
  try { endpoint = new URL(connection.endpoint); } catch { return null; }
  if (endpoint.protocol !== "http:" || endpoint.hostname !== "127.0.0.1" || !endpoint.port
      || endpoint.username || endpoint.password || endpoint.search || endpoint.hash
      || endpoint.pathname !== `/mcp/${profile}`) return null;
  return connection;
}

async function healthyConnection(
  runtimeId = readInstalledRuntimeId(),
  runtime = readInstalledRuntime(runtimeId),
) {
  const connection = readConnection();
  if (connection === null) return null;
  if (runtimeId === null || connection.runtime_id !== runtimeId
      || runtime === null || !supportsRuntimeContract(runtime.contractVersion) || !matchesRelease(runtimeId, runtime)) return null;
  try {
    const response = await fetch(connection.endpoint, {
      method: "POST",
      headers: { "Authorization": ["Bearer", connection.bearer_token].join(" "), "Content-Type": "application/json", "MCP-Protocol-Version": MCP_PROTOCOL_VERSION, "X-EdgePilot-Runtime-ID": runtimeId },
      body: JSON.stringify({ jsonrpc: "2.0", id: "bridge-probe", method: "initialize", params: { protocolVersion: MCP_PROTOCOL_VERSION } }),
      redirect: "error",
      signal: AbortSignal.timeout(500),
    });
    const body = response.ok ? await response.json() : null;
    return body?.result?.serverInfo?.runtimeId === runtimeId && body.result.serverInfo.runtimeReady !== false
      && readInstalledRuntimeId() === runtimeId ? connection : null;
  } catch { return null; }
}

function matchesRelease(runtimeId, runtime) {
  return runtime.releaseVersion === productVersion && (delivery.expected_runtime_ids.length === 0 || delivery.expected_runtime_ids.includes(runtimeId));
}

function toolResult(value, isError = false) {
  isError ||= value?.state === "error" || value?.state === "stale_session";
  const pending = value?.message === "runtime_operation_pending";
  const text = value?.state === "awaiting_confirmation"
    ? "The target Runtime is prepared. Show the listed old processes/tasks and ask once: defer the switch, or stop the listed old version and continue. Stopping programs does not guarantee cancelling orders or closing positions. Only after that explicit choice call the same lifecycle tool with action, operation_id and snapshot_digest. Do not open the old Dashboard or onboarding as target success."
    : value?.state === "deferred" ? "Runtime switch deferred. The old environment is preserved; this target startup request has ended."
    : pending
    ? "EdgePilot Runtime installation or startup is still pending. Wait for runtime_status to report ready with connection_ready=true before opening Dashboard or onboarding. Do not start another installation."
    : isError || (typeof value?.state === "string" && value.state !== "ready")
      ? "EdgePilot Runtime is not ready. Follow required_action before opening Dashboard or onboarding."
      : value?.state === "ready"
        ? "EdgePilot Runtime is ready. Installation and startup have completed; Dashboard and onboarding may now be opened."
        : "EdgePilot Runtime operation completed.";
  return {
    content: [{ type: "text", text }],
    structuredContent: value,
    ...(isError ? { isError: true } : {}),
  };
}

function resultResponse(id, result) { return { jsonrpc: "2.0", id, result }; }
function errorResponse(id, code, message) { return { jsonrpc: "2.0", id, error: { code, message } }; }
function writeResponse(value) { process.stdout.write(`${JSON.stringify(value)}\n`); }

function requireEmpty(value) {
  if (Object.keys(value).length !== 0) throw new BridgeError("invalid_arguments");
}

function switchArguments(value) {
  if (Object.keys(value).length === 0) return {};
  if (Object.keys(value).sort().join(",") !== "action,operation_id,snapshot_digest"
      || !["defer", "stop_and_continue"].includes(value.action) || !/^[0-9a-f-]{36}$/.test(value.operation_id)
      || !/^sha256:[0-9a-f]{64}$/.test(value.snapshot_digest)) throw new BridgeError("invalid_arguments");
  return { "switch-action": value.action, "operation-id": value.operation_id, "snapshot-digest": value.snapshot_digest };
}

function fatal(code) {
  process.stderr.write(`EdgePilot MCP bridge: ${code}\n`);
  process.exit(1);
}

function readDelivery() {
  let value;
  try { value = JSON.parse(readFileSync(join(root, "delivery.json"), "utf8")); } catch { throw new BridgeError("delivery_config_missing"); }
  if (value?.schema !== "edgepilot-delivery-v1" || value.product !== profile || !new Set(["local", "production"]).has(value.environment) || typeof value.channel_url !== "string" || value.compatibility !== "release" || value.expected_product_version !== productVersion || Object.keys(value).sort().join(",") !== "channel_url,compatibility,environment,expected_product_version,expected_runtime_ids,marketplace_origin,product,schema" || (profile === "live" ? typeof value.marketplace_origin !== "string" : value.marketplace_origin !== null)) throw new BridgeError("delivery_config_invalid");
  if (!Array.isArray(value.expected_runtime_ids) || value.expected_runtime_ids.some((id) => typeof id !== "string" || !/^sha256:[0-9a-f]{64}$/.test(id)) || new Set(value.expected_runtime_ids).size !== value.expected_runtime_ids.length) throw new BridgeError("delivery_config_invalid");
  if (value.expected_runtime_ids.length === 0 && productVersion !== "0.0.0" && !existsSync(join(root, ".edgepilot-connection.json"))) throw new BridgeError("runtime_binding_missing");
  validateDownloadUrl(value.channel_url);
  if ((value.environment === "local") !== (new URL(value.channel_url).hostname === "127.0.0.1")) throw new BridgeError("delivery_environment_invalid");
  if (value.marketplace_origin !== null) validateDownloadUrl(value.marketplace_origin);
  return value;
}

function readPluginVersion() {
  try {
    const value = JSON.parse(readFileSync(join(root, ".codex-plugin", "plugin.json"), "utf8"));
    return typeof value.version === "string" ? value.version : "0.0.0";
  } catch { return "0.0.0"; }
}

function configuredBootstrapPath() {
  const configured = process.env.EDGEPILOT_BOOTSTRAP_PATH;
  if (configured !== undefined) {
    if (!isAbsolute(configured)) throw new BridgeError("bootstrap_path_invalid");
    return configured;
  }
  return join(root, "bootstrap.mjs");
}

function lifecycleProcessEnvironment() {
  const environment = Object.fromEntries(Object.entries(process.env).filter(([key]) => new Set(["SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "LANG", "LC_ALL", "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "EDGEPILOT_ENV", "EDGEPILOT_LIVE_DASHBOARD_PORT", "EDGEPILOT_RESEARCH_DASHBOARD_PORT", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "EDGEPILOT_PROXY_URL", "EDGEPILOT_PROXY_MODE"]).has(key.toUpperCase())));
  const names = Object.keys(environment).filter((key) => key.toUpperCase() === "NO_PROXY");
  if (names.length === 0) environment.NO_PROXY = "127.0.0.1";
  else for (const name of names) {
    const raw = String(environment[name] ?? "");
    const tokens = raw.split(",").map((item) => item.trim()).filter(Boolean);
    if (!tokens.some((item) => item.toLowerCase() === "127.0.0.1")) {
      environment[name] = ["127.0.0.1", ...tokens].join(",");
    }
  }
  return environment;
}

async function runLifecycleProcess(args) {
  const logs = join(runtimeHome, "runtime", "logs");
  mkdirSync(logs, { recursive: true, mode: 0o700 });
  const id = randomUUID();
  const output = join(logs, `operation-${id}.out`), errors = join(logs, `operation-${id}.err`);
  const out = openSync(output, "wx", 0o600), err = openSync(errors, "wx", 0o600);
  let child;
  try {
    child = spawn(process.execPath, args, {
      detached: true, stdio: ["ignore", out, err], windowsHide: true,
      env: lifecycleProcessEnvironment(),
    });
  } finally { closeSync(out); closeSync(err); }
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { child.unref(); reject(new BridgeError("runtime_operation_pending")); }, 20 * 60_000);
    child.once("error", () => { clearTimeout(timer); reject(new BridgeError("runtime_start_failed")); });
    child.once("close", status => {
      clearTimeout(timer);
      const bounded = path => lstatSync(path).size <= 1024 * 1024 ? readFileSync(path, "utf8") : "";
      resolve({ status, stdout: bounded(output), stderr: bounded(errors) });
      rmSync(output, { force: true }); rmSync(errors, { force: true });
    });
  });
}

function validateBootstrapFile(path) {
  let metadata;
  try { metadata = lstatSync(path); } catch { throw new BridgeError("bootstrap_missing"); }
  if (!metadata.isFile() || metadata.isSymbolicLink() || metadata.size < 1 || metadata.size > MAX_BOOTSTRAP_BYTES) throw new BridgeError("bootstrap_path_invalid");
  return path;
}

function validateDownloadUrl(value) {
  let parsed;
  try { parsed = new URL(value); } catch { throw new BridgeError("download_url_invalid"); }
  if (!["http:", "https:"].includes(parsed.protocol) || parsed.username || parsed.password) throw new BridgeError("download_url_invalid");
}

const input = createInterface({ input: process.stdin, crlfDelay: Infinity, terminal: false });
for await (const line of input) {
  if (!line.trim()) continue;
  if (Buffer.byteLength(line, "utf8") > MAX_MESSAGE_BYTES) {
    writeResponse(errorResponse(null, -32600, "request_too_large"));
    continue;
  }
  let request;
  try { request = JSON.parse(line); } catch {
    writeResponse(errorResponse(null, -32700, "invalid_json"));
    continue;
  }
  try {
    const response = await handleRequest(request);
    if (response !== null) writeResponse(response);
  } catch (error) {
    const code = error instanceof BridgeError ? error.code : "bridge_internal_failure";
    if (!(error instanceof BridgeError)) process.stderr.write(`EdgePilot MCP bridge diagnostic: ${error instanceof Error ? `${error.name}:${error.message}` : "unknown"}\n`);
    writeResponse(errorResponse(request?.id ?? null, -32603, code));
  }
}
