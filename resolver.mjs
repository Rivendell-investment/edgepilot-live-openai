import { createHash, randomUUID } from "node:crypto";
import { chmodSync, existsSync, lstatSync, mkdirSync, readFileSync, renameSync, rmSync, writeFileSync } from "node:fs";
import { createInterface } from "node:readline";
import { fileURLToPath } from "node:url";
import { homedir } from "node:os";
import { dirname, isAbsolute, join } from "node:path";
import { spawnSync } from "node:child_process";

const MAX_MESSAGE_BYTES = 256 * 1024;
const MAX_CHANNEL_BYTES = 512 * 1024;
const MAX_BOOTSTRAP_BYTES = 2 * 1024 * 1024;
const MCP_PROTOCOL_VERSION = "2025-06-18";
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
const configuredRoot = process.env.EDGEPILOT_PLUGIN_STATE_ROOT;
if (configuredRoot !== undefined && !isAbsolute(configuredRoot)) fatal("invalid_connection_root");
const runtimeDirectory = profile === "live" ? ".edgepilot-runtime-live" : ".edgepilot-runtime-research";
const pluginStateRoot = configuredRoot ?? join(homedir(), runtimeDirectory, "plugins");
const runtimeHome = configuredRoot === undefined ? join(homedir(), runtimeDirectory) : dirname(configuredRoot);
const sharedConnection = join(pluginStateRoot, "connections", `${profile}.json`);
const adjacentConnection = join(root, ".edgepilot-connection.json");
const pluginVersion = readPluginVersion();
const productVersion = pluginVersion.split("+", 1)[0];

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
    if (name === "edgepilot_strategy_recommend") return resultResponse(id, await executeHostOperation("catalog.strategy.recommend", argumentsValue));
    if (name === "edgepilot_dashboard_open") {
      requireEmpty(argumentsValue);
      return resultResponse(id, await executeHostOperation("dashboard.open", {}));
    }
    if (name in LIFECYCLE_HANDLERS) {
      const result = await LIFECYCLE_HANDLERS[name](argumentsValue);
      const response = resultResponse(id, toolResult(result));
      if (name !== "edgepilot_runtime_status" && result.state === "ready") {
        setTimeout(() => writeResponse({ jsonrpc: "2.0", method: "notifications/tools/list_changed" }), 0);
      }
      return response;
    }
    if (!HOST_TOOL_NAMES.has(name)) return errorResponse(id, -32601, "tool_not_found");
    return forwardHost(request);
  }
  if (request.method === "resources/list") {
    const connection = await healthyConnection();
    if (connection === null) return resultResponse(id, { resources: [] });
    return forward(connection, request);
  }
  if (request.method === "resources/read") {
    const connection = await healthyConnection();
    if (connection === null) return errorResponse(id, -32001, "runtime_not_ready");
    return forward(connection, request);
  }
  return errorResponse(id, -32601, "method_not_found");
}

const LIFECYCLE_HANDLERS = {
  edgepilot_runtime_status: async (argumentsValue) => {
    requireEmpty(argumentsValue);
    return await runtimeStatus();
  },
  edgepilot_runtime_start: async (argumentsValue) => {
    requireEmpty(argumentsValue);
    return runLifecycle("ensure-start");
  },
  edgepilot_runtime_update: async (argumentsValue) => {
    requireEmpty(argumentsValue);
    return runLifecycle("update");
  },
  edgepilot_runtime_repair: async (argumentsValue) => {
    requireEmpty(argumentsValue);
    return runLifecycle("repair");
  },
};

async function listTools() {
  const lifecycle = lifecycleTools();
  const connection = await healthyConnection();
  if (connection === null) return lifecycle;
  const response = await forward(connection, { jsonrpc: "2.0", id: "bridge-tools", method: "tools/list", params: {} });
  const tools = response?.result?.tools;
  return Array.isArray(tools) ? [...lifecycle, ...tools] : lifecycle;
}

function lifecycleTools() {
  const emptyInput = { type: "object", properties: {}, required: [], additionalProperties: false };
  const output = {
    type: "object",
    properties: {
      state: { enum: ["ready", "stopped", "not_installed", "error"] },
      profile: { enum: ["live", "research"] },
      plugin_version: { type: "string" },
      version: { type: "string" },
      expected_product_version: { type: "string" },
      runtime_version: { type: ["string", "null"] },
      runtime_id: { type: ["string", "null"] },
      bootstrap_installed: { type: "boolean" },
      connection_ready: { type: "boolean" },
      message: { type: ["string", "null"] },
    },
    required: ["state", "profile", "version", "plugin_version", "expected_product_version", "runtime_version", "runtime_id", "bootstrap_installed", "connection_ready", "message"],
    additionalProperties: false,
  };
  const definitions = [
    ["edgepilot_runtime_status", "Runtime Status", "Inspect local EdgePilot Runtime and Host readiness without starting them.", true],
    ["edgepilot_runtime_start", "Runtime Start", "Download/install when needed and start the local EdgePilot Runtime Host.", false],
    ["edgepilot_runtime_update", "Runtime Update", "Update to the channel Runtime and restart the local Host.", false],
    ["edgepilot_runtime_repair", "Runtime Repair", "Reinstall the channel Runtime and restart the local Host.", false],
  ];
  const tools = definitions.map(([name, title, description, readOnly]) => ({
    name, title, description, inputSchema: emptyInput, outputSchema: output,
    annotations: { readOnlyHint: readOnly, destructiveHint: false, idempotentHint: true, openWorldHint: !readOnly },
  }));
  tools.push({
    name: "edgepilot_strategy_recommend", title: "Recommend Strategies",
    description: "Return profile-scoped strategy recommendations from the Runtime owner.",
    inputSchema: recommendationSchema(),
    annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: true },
  });
  tools.push({
    name: "edgepilot_dashboard_open", title: "Open Dashboard",
    description: "Start or reuse the Host-owned profile Dashboard and return its loopback URL.",
    inputSchema: emptyInput,
    annotations: { readOnlyHint: false, destructiveHint: false, idempotentHint: true, openWorldHint: false },
  });
  return tools;
}

function recommendationSchema() {
  return {
    type: "object",
    properties: {
      questionnaire_version: { const: "2.0" },
      profit_style: { enum: ["trend", "reversal", "relative_value"] },
      holding_period: { enum: ["intraday", "multi_day", "multi_week"] },
      pain_point: { enum: ["loss_streak", "inactivity", "tail_loss"] },
      max_drawdown_pct: { enum: [5, 15, 20] },
      trading_mode: { enum: ["long_only_no_leverage", "long_short_low_leverage", "long_short_high_leverage"] },
      allocation_band: { enum: ["under_25k", "25k_100k", "over_100k"] },
      universe: { enum: ["majors", "altcoins", "any"] },
      locale: { enum: ["en", "ko", "zh-CN", "zh-TW"] },
    },
    required: ["questionnaire_version", "profit_style", "holding_period", "pain_point", "max_drawdown_pct", "trading_mode", "allocation_band", "universe", "locale"],
    additionalProperties: false,
  };
}

async function executeHostOperation(operationId, argumentsValue) {
  const connection = await healthyConnection();
  if (connection === null) return toolResult({ ...(await runtimeStatus()), message: "runtime_not_ready" }, true);
  const get = await forward(connection, { jsonrpc: "2.0", id: "bridge-operation-get", method: "tools/call", params: { name: "edgepilot_tool_get", arguments: { operation_ids: [operationId], include_output_schema: false } } });
  const operation = get?.result?.structuredContent?.operations?.[0];
  if (operation?.id !== operationId || typeof operation.schema_revision !== "string") throw new BridgeError("operation_unavailable");
  const execute = await forward(connection, { jsonrpc: "2.0", id: "bridge-operation-execute", method: "tools/call", params: { name: "edgepilot_tool_execute", arguments: { calls: [{ call_id: `bridge-${operationId.replaceAll(".", "-")}`, operation_id: operationId, schema_revision: operation.schema_revision, authority: "direct", arguments: argumentsValue }], presentation: "never" } } });
  if (execute?.error) throw new BridgeError(String(execute.error.message ?? "operation_failed"));
  return execute.result;
}

async function runLifecycle(command) {
  const delivery = readDelivery();
  const channelUrl = process.env.EDGEPILOT_CHANNEL_URL ?? delivery.channel_url;
  const bootstrap = await ensureBootstrap(channelUrl, runtimeHome);
  const args = [bootstrap, command, "--runtime-home", runtimeHome, "--channel-url", channelUrl, "--product", profile, "--plugin-version", pluginVersion, "--expected-product-version", delivery.expected_product_version];
  const channel = new URL(channelUrl);
  args.push("--environment", channel.protocol === "http:" && new Set(["127.0.0.1", "localhost", "::1"]).has(channel.hostname) ? "local" : "production");
  if (delivery.marketplace_origin !== null) args.push("--marketplace-origin", delivery.marketplace_origin);
  const completed = spawnSync(process.execPath, args, {
    encoding: "utf8",
    windowsHide: true,
    timeout: 300_000,
    env: Object.fromEntries(Object.entries(process.env).filter(([key]) => new Set(["SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "LANG", "LC_ALL", "EDGEPILOT_ENV", "EDGEPILOT_MARKETPLACE_ORIGIN", "EDGEPILOT_LIVE_DASHBOARD_PORT", "EDGEPILOT_RESEARCH_DASHBOARD_PORT"]).has(key.toUpperCase()))),
  });
  if (completed.error?.code === "ETIMEDOUT") throw new BridgeError("runtime_timeout");
  if (completed.status !== 0) {
    const code = /^EdgePilot bootstrap: ([a-z0-9_]+)$/mu.exec(completed.stderr ?? "")?.[1] ?? "bootstrap_failed";
    return { ...(await runtimeStatus()), state: "error", message: code };
  }
  const connection = await healthyConnection();
  if (connection === null) return { ...(await runtimeStatus()), state: "error", message: "host_not_ready" };
  return { ...(await runtimeStatus()), state: "ready", connection_ready: true, message: null };
}

async function runtimeStatus() {
  const runtimeId = readInstalledRuntimeId();
  const bootstrap = configuredBootstrapPath();
  const connection = await healthyConnection();
  return {
    state: connection === null ? (runtimeId === null ? "not_installed" : "stopped") : "ready",
    profile,
    version: productVersion,
    plugin_version: pluginVersion,
    expected_product_version: readDelivery().expected_product_version,
    runtime_version: readInstalledRuntimeVersion(runtimeId),
    runtime_id: connection?.runtime_id ?? runtimeId,
    bootstrap_installed: existsSync(bootstrap),
    connection_ready: connection !== null,
    message: null,
  };
}

function readInstalledRuntimeId() {
  try {
    const value = JSON.parse(readFileSync(join(runtimeHome, "runtime", "current.json"), "utf8"));
    return typeof value.current_runtime_id === "string" ? value.current_runtime_id : null;
  } catch { return null; }
}

function readInstalledRuntimeVersion(runtimeId) {
  if (runtimeId === null || !/^sha256:[0-9a-f]{64}$/.test(runtimeId)) return null;
  try {
    const manifest = JSON.parse(readFileSync(join(runtimeHome, "runtime", "releases", runtimeId.slice(7), "RUNTIME.json"), "utf8"));
    return typeof manifest?.payload?.release_version === "string" ? manifest.payload.release_version : null;
  } catch { return null; }
}

async function forwardHost(request) {
  const connection = await healthyConnection();
  if (connection === null) {
    return resultResponse(request.id, toolResult({ ...(await runtimeStatus()), message: "runtime_not_ready" }, true));
  }
  return forward(connection, request);
}

async function forward(connection, request) {
  const controller = new AbortController();
  const deadline = setTimeout(() => controller.abort(), 120_000);
  try {
    const response = await fetch(connection.endpoint, {
      method: "POST",
      headers: {
        "Authorization": ["Bearer", connection.bearer_token].join(" "),
        "Content-Type": "application/json",
        "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
      },
      body: JSON.stringify(request),
      signal: controller.signal,
      redirect: "error",
    });
    if (response.status === 202) return null;
    const body = await response.text();
    if (Buffer.byteLength(body, "utf8") > MAX_MESSAGE_BYTES) throw new BridgeError("response_too_large");
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

async function healthyConnection() {
  const connection = readConnection();
  if (connection === null) return null;
  try {
    const response = await fetch(connection.endpoint, {
      method: "POST",
      headers: { "Authorization": ["Bearer", connection.bearer_token].join(" "), "Content-Type": "application/json", "MCP-Protocol-Version": MCP_PROTOCOL_VERSION },
      body: JSON.stringify({ jsonrpc: "2.0", id: "bridge-probe", method: "ping", params: {} }),
      redirect: "error",
      signal: AbortSignal.timeout(500),
    });
    return response.ok ? connection : null;
  } catch { return null; }
}

function toolResult(value, isError = false) {
  return {
    content: [{ type: "text", text: isError ? "EdgePilot Runtime is not ready." : "EdgePilot Runtime lifecycle completed." }],
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

function fatal(code) {
  process.stderr.write(`EdgePilot MCP bridge: ${code}\n`);
  process.exit(1);
}

function readDelivery() {
  let value;
  try { value = JSON.parse(readFileSync(join(root, "delivery.json"), "utf8")); } catch { throw new BridgeError("delivery_config_missing"); }
  if (value?.schema !== "edgepilot-delivery-v2" || value.product !== profile || typeof value.channel_url !== "string" || value.compatibility !== "exact" || value.expected_product_version !== productVersion || Object.keys(value).sort().join(",") !== "channel_url,compatibility,expected_product_version,marketplace_origin,product,schema" || (profile === "live" ? typeof value.marketplace_origin !== "string" : value.marketplace_origin !== null)) throw new BridgeError("delivery_config_invalid");
  validateDownloadUrl(value.channel_url);
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
  return join(runtimeHome, "bootstrap", "bootstrap.mjs");
}

async function ensureBootstrap(channelUrl, home) {
  const configured = process.env.EDGEPILOT_BOOTSTRAP_PATH;
  if (configured !== undefined) return validateBootstrapFile(configuredBootstrapPath());
  const directory = join(home, "bootstrap");
  const bootstrap = join(directory, "bootstrap.mjs");
  let channel;
  try { channel = await downloadJson(channelUrl, MAX_CHANNEL_BYTES); } catch {
    if (existsSync(bootstrap)) return validateBootstrapFile(bootstrap);
    throw new BridgeError("bootstrap_download_failed");
  }
  const info = channel?.bootstrap;
  if (channel?.schema !== "edgepilot-channel-v1" || channel.product !== profile || typeof info !== "object" || info === null
      || typeof info.url !== "string" || !Number.isSafeInteger(info.size) || info.size < 1 || info.size > MAX_BOOTSTRAP_BYTES
      || typeof info.sha256 !== "string" || !/^sha256:[0-9a-f]{64}$/.test(info.sha256)) throw new BridgeError("channel_invalid");
  validateDownloadUrl(info.url);
  if (existsSync(bootstrap) && digest(readFileSync(bootstrap)) === info.sha256) return validateBootstrapFile(bootstrap);
  const bytes = await downloadBytes(info.url, info.size);
  if (bytes.length !== info.size || digest(bytes) !== info.sha256) throw new BridgeError("bootstrap_digest_mismatch");
  mkdirSync(directory, { recursive: true, mode: 0o700 });
  const temporary = join(directory, `.bootstrap.${randomUUID()}.tmp.mjs`);
  try {
    writeFileSync(temporary, bytes, { flag: "wx", mode: 0o700 });
    const checked = spawnSync(process.execPath, ["--check", temporary], { encoding: "utf8", windowsHide: true, timeout: 10_000 });
    if (checked.status !== 0) throw new BridgeError("bootstrap_syntax_invalid");
    renameSync(temporary, bootstrap);
    if (process.platform !== "win32") chmodSync(bootstrap, 0o700);
  } finally { rmSync(temporary, { force: true }); }
  return validateBootstrapFile(bootstrap);
}

function validateBootstrapFile(path) {
  let metadata;
  try { metadata = lstatSync(path); } catch { throw new BridgeError("bootstrap_missing"); }
  if (!metadata.isFile() || metadata.isSymbolicLink() || metadata.size < 1 || metadata.size > MAX_BOOTSTRAP_BYTES) throw new BridgeError("bootstrap_path_invalid");
  return path;
}

async function downloadJson(url, maximumBytes) {
  const bytes = await downloadBytes(url, maximumBytes);
  try { return JSON.parse(bytes.toString("utf8")); } catch { throw new BridgeError("channel_invalid"); }
}

async function downloadBytes(url, maximumBytes) {
  validateDownloadUrl(url);
  const response = await fetch(url, { redirect: "error", signal: AbortSignal.timeout(10_000) });
  if (!response.ok) throw new BridgeError("download_failed");
  const bytes = Buffer.from(await response.arrayBuffer());
  if (bytes.length < 1 || bytes.length > maximumBytes) throw new BridgeError("download_size_invalid");
  return bytes;
}

function validateDownloadUrl(value) {
  let parsed;
  try { parsed = new URL(value); } catch { throw new BridgeError("download_url_invalid"); }
  if (!["http:", "https:"].includes(parsed.protocol) || parsed.username || parsed.password) throw new BridgeError("download_url_invalid");
}

function digest(value) {
  return `sha256:${createHash("sha256").update(value).digest("hex")}`;
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
