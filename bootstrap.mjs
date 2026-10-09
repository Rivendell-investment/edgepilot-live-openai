#!/usr/bin/env node
// Dependency-free installer/launcher for the EdgePilot Runtime.

import { createHash, createPublicKey, verify as verifySignature, randomUUID } from "node:crypto";
import {
  chmodSync,
  closeSync,
  constants,
  createReadStream,
  createWriteStream,
  existsSync,
  fstatSync,
  mkdirSync,
  openSync,
  readdirSync,
  lstatSync,
  readFileSync,
  realpathSync,
  readSync,
  renameSync,
  rmSync,
  statSync,
  writeFileSync,
} from "node:fs";
import { readFile } from "node:fs/promises";
import { homedir, platform as hostPlatform, arch as hostArch, release as hostRelease } from "node:os";
import { basename, dirname, isAbsolute, join, resolve, sep, toNamespacedPath } from "node:path";
import { fileURLToPath } from "node:url";
import { spawn, spawnSync } from "node:child_process";
import { Transform, Readable } from "node:stream";
import { pipeline } from "node:stream/promises";
import { createInflateRaw } from "node:zlib";
import * as __edgepilot_lifecycle_dependency_0 from "node:fs";
import * as __edgepilot_lifecycle_dependency_1 from "node:path";
import * as __edgepilot_lifecycle_dependency_2 from "node:crypto";
import * as __edgepilot_lifecycle_dependency_3 from "node:child_process";
const { LifecycleTransaction, readLifecycleState, writeLifecycleState, runLiveMaintenance, switchSelection, selectionCovers } = (() => {
// Durable lifecycle journal and bounded bundled-Python maintenance transport.
const { closeSync, existsSync, fsyncSync, lstatSync, mkdirSync, openSync, readFileSync, renameSync, rmSync, writeFileSync } = __edgepilot_lifecycle_dependency_0;
const { dirname, join } = __edgepilot_lifecycle_dependency_1;
const { createHash, randomUUID } = __edgepilot_lifecycle_dependency_2;
const { spawn } = __edgepilot_lifecycle_dependency_3;

function lifecycleError(code) { return Object.assign(new Error(code), { code }); }

function writeLifecycleState(path, value) {
  mkdirSync(dirname(path), { recursive: true, mode: 0o700 });
  if (existsSync(path) && lstatSync(path).isSymbolicLink()) throw lifecycleError("runtime_state_invalid");
  const temporary = `${path}.${randomUUID()}.tmp`;
  const fd = openSync(temporary, "wx", 0o600);
  try {
    writeFileSync(fd, JSON.stringify(value) + "\n");
    fsyncSync(fd);
  } finally { closeSync(fd); }
  try { renameSync(temporary, path); } finally { rmSync(temporary, { force: true }); }
}

function readLifecycleState(root) {
  const path = join(root, "lifecycle.json");
  if (!existsSync(path)) return null;
  const metadata = lstatSync(path);
  if (!metadata.isFile() || metadata.isSymbolicLink() || metadata.size > 65536) throw lifecycleError("lifecycle_state_invalid");
  let state;
  try { state = JSON.parse(readFileSync(path, "utf8")); } catch { throw lifecycleError("lifecycle_state_invalid"); }
  if (state?.schema !== "edgepilot-lifecycle-v1" || !/^[0-9a-f-]{36}$/.test(state.operation_id)
      || !["prepare", "inspect", "awaiting_confirmation", "deferred", "quiesce", "retire", "migrate", "start", "commit", "ready", "blocked", "repair_required"].includes(state.phase)
      || !/^\d+\.\d+\.\d+$/.test(state.target_version) || !Array.isArray(state.target_ids)
      || state.target_ids.some(id => !/^sha256:[0-9a-f]{64}$/.test(id))
      || (state.retired_directory !== undefined && !/^[0-9a-f]{64}-[0-9a-f-]{36}$/.test(state.retired_directory))) throw lifecycleError("lifecycle_state_invalid");
  for (const selection of [state.selection, state.authorized]) {
    if (selection == null) continue;
    if (selection.operation_id !== state.operation_id || selection.target_version !== state.target_version
        || !state.target_ids.includes(selection.target_runtime_id) || !/^sha256:[0-9a-f]{64}$/.test(selection.snapshot_digest)
        || !["live", "research"].includes(selection.product) || !["local", "production"].includes(selection.environment)
        || !Array.isArray(selection.processes) || !Array.isArray(selection.jobs)
        || selection.processes.length > 200 || selection.jobs.length > 200
        || selection.processes.some(item => !Number.isSafeInteger(item.pid) || item.pid <= 0 || typeof item.birth !== "string" || !item.birth))
      throw lifecycleError("lifecycle_state_invalid");
  }
  return state;
}

class LifecycleTransaction {
  constructor(root, targetVersion, targetIds) {
    this.root = root;
    const previous = readLifecycleState(root);
    const same = previous?.target_version === targetVersion && JSON.stringify(previous.target_ids) === JSON.stringify(targetIds);
    this.value = {
      schema: "edgepilot-lifecycle-v1", operation_id: same && previous.phase !== "ready" ? previous.operation_id : randomUUID(),
      target_version: targetVersion, target_ids: targetIds, phase: "prepare", last_error: null,
      started_at: same && previous.phase !== "ready" ? previous.started_at : new Date().toISOString(),
      cutover_started: previous?.cutover_started === true && previous.phase !== "ready",
      ...(same && previous.phase !== "ready" ? { selection: previous.selection ?? null, authorized: previous.authorized ?? null } : {}),
      updated_at: new Date().toISOString(),
      ...(previous?.retired_directory ? { retired_directory: previous.retired_directory } : {}),
    };
    this.advance("prepare");
  }
  advance(phase, extra = {}) {
    this.value = { ...this.value, ...extra, phase, updated_at: new Date().toISOString() };
    writeLifecycleState(join(this.root, "lifecycle.json"), this.value);
  }
  failure(error) {
    const interruptedPhase = this.value.phase;
    this.advance(error?.code === "runtime_pinned" ? "blocked" : "repair_required", {
      interrupted_phase: interruptedPhase, last_error: /^[a-z][a-z0-9_]{0,100}$/.test(error?.code) ? error.code : "lifecycle_failed",
    });
  }
}

// The caller holds the existing lifecycle lock only while creating or checking this
// snapshot, never while the user considers the choice.
function switchSelection(transaction, { product, environment, runtimeId, processes, jobs }) {
  const snapshot = { product, environment, runtime_id: runtimeId,
    processes: [...processes].sort((a, b) => a.pid - b.pid),
    jobs: [...jobs].sort((a, b) => a.job_ref.localeCompare(b.job_ref)) };
  const digest = `sha256:${createHash("sha256").update(JSON.stringify(snapshot)).digest("hex")}`;
  const selection = { operation_id: transaction.value.operation_id, target_version: transaction.value.target_version,
    target_runtime_id: transaction.value.target_runtime_id, snapshot_digest: digest, ...snapshot };
  if (Buffer.byteLength(JSON.stringify(selection)) > 24 * 1024) throw lifecycleError("runtime_process_inventory_exceeded");
  transaction.advance("awaiting_confirmation", { selection, authorized: null });
  return selection;
}

function selectionCovers(selection, { processes, jobs }) {
  return selection && Array.isArray(selection.processes) && Array.isArray(selection.jobs)
    && processes.every(item => selection.processes.some(old => old.pid === item.pid && old.birth === item.birth))
    && jobs.every(item => selection.jobs.some(old => old.job_ref === item.job_ref && old.runtime_id === item.runtime_id));
}

async function runLiveMaintenance({ python, liveStateRoot, runtimeId, operation = "inspect", jobRef, accountRef, idempotencyKey, env }) {
  const args = ["-I", "-B", "-m", "edgepilot.runtime_maintenance", operation, "--state-root", liveStateRoot, "--runtime-id", runtimeId];
  for (const [name, value] of [["job-ref", jobRef], ["account-ref", accountRef], ["idempotency-key", idempotencyKey]]) {
    if (value !== undefined) args.push(`--${name}`, value);
  }
  return new Promise((resolve, reject) => {
    const child = spawn(python, args, { stdio: ["ignore", "pipe", "pipe"], windowsHide: true, env });
    let output = "";
    let overflow = false;
    child.stdout.on("data", chunk => {
      if (overflow) return;
      output += chunk.toString("utf8");
      if (Buffer.byteLength(output) > 1024 * 1024) { overflow = true; child.kill(); }
    });
    child.stderr.resume(); // Never forward interpreter errors containing paths or state.
    const timer = setTimeout(() => child.kill("SIGKILL"), operation === "stop" ? 40000 : 30000);
    child.once("error", () => { clearTimeout(timer); reject(lifecycleError("maintenance_start_failed")); });
    child.once("close", (code) => {
      clearTimeout(timer);
      if (overflow) return reject(lifecycleError("maintenance_output_exceeded"));
      let value;
      try { value = JSON.parse(output); } catch { return reject(lifecycleError("maintenance_unavailable")); }
      if (code !== 0) return reject(lifecycleError(/^[a-z][a-z0-9_]+$/.test(value?.error?.code) ? value.error.code : "maintenance_failed"));
      if (value?.schema !== "edgepilot-live-maintenance-v1" || !Array.isArray(value.jobs) || !Array.isArray(value.pinned_runtime_ids)) return reject(lifecycleError("maintenance_response_invalid"));
      resolve(value);
    });
  });
}
return { LifecycleTransaction, readLifecycleState, writeLifecycleState, runLiveMaintenance, switchSelection, selectionCovers };
})();
import * as __edgepilot_processes_dependency_0 from "node:child_process";
import * as __edgepilot_processes_dependency_1 from "node:path";
const { runtimeProcessesByRole, stopAuthorizedProcesses, runtimeExecutablesInUse } = (() => {
// Inventory and stop processes running a verified Runtime interpreter.
const { spawnSync } = __edgepilot_processes_dependency_0;
const { resolve, join } = __edgepilot_processes_dependency_1;

function processFailure(code) { return Object.assign(new Error(code), { code }); }

function classifyRuntimeCommand(executable, command) {
  const prefixes = [executable + " ", '"' + executable + '" '];
  const prefix = prefixes.find(value => command.startsWith(value));
  if (!prefix) return null;
  const args = command.slice(prefix.length);
  if (/^(?:-(?:I|B|u)\s+)*-m\s+edgepilot_runtime_host\.host_main(?:\s|$)/u.test(args)) return "host";
  if (/^(?:-(?:I|B|u)\s+)*-m\s+edgepilot_worker\.worker_main(?:\s|$)/u.test(args)) return "worker";
  if (/^(?:-(?:I|B|u)\s+)*-m\s+edgepilot\.tradingd(?:\s|$)/u.test(args)) return "tradingd";
  if (/^(?:-(?:I|B|u)\s+)*-m\s+edgepilot\.trading_engine(?:\s|$)/u.test(args)) return "engine";
  // Runtimes installed before the Live entry was renamed still run `_serve_unmanaged_for_test`;
  // upgrades must recognize their Dashboards to stop them.
  if (/^(?:-(?:I|B|u)\s+)*-c\s+["']?from (?:edgepilot\.dashboard\.http import (?:serve|_serve_unmanaged_for_test)|edgepilot_research\.ui import serve)(?:\s|;|$)/u.test(args)) return "dashboard";
  return null;
}

function processInventory({ executables = [] } = {}) {
  if (process.platform === "win32") {
    const powershell = join(process.env.SYSTEMROOT ?? "C:\\Windows", "System32", "WindowsPowerShell", "v1.0", "powershell.exe");
    const scope = JSON.stringify({ executables }).replaceAll("'", "''");
    const script = `$ErrorActionPreference='Stop';
$scope=ConvertFrom-Json '${scope}';
$me=[System.Security.Principal.WindowsIdentity]::GetCurrent().Name;
@(Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object {
  $line=$_.CommandLine; $exact=$false;
  foreach ($exe in $scope.executables) {
    if ($line -and ($line.StartsWith($exe+' ', [StringComparison]::Ordinal) -or $line.StartsWith('"'+$exe+'" ', [StringComparison]::Ordinal))) {$exact=$true; break}
  };
  $exact
} | ForEach-Object {
  $o=Invoke-CimMethod -InputObject $_ -MethodName GetOwner -ErrorAction Stop;
  if ($o.ReturnValue -ne 0) {throw 'process_owner_unverified'};
  if (($o.Domain+'\\'+$o.User) -eq $me) {$_ | Select-Object ProcessId,ParentProcessId,CommandLine}
}) | ConvertTo-Json -Compress`;
    const result = spawnSync(powershell, ["-NoProfile", "-NonInteractive", "-Command", script], { encoding: "utf8", timeout: 120000, maxBuffer: 4 * 1024 * 1024, windowsHide: true });
    if (result.status !== 0) throw processFailure("runtime_process_inspection_failed");
    try { const value = JSON.parse(result.stdout.trim() || "[]"); return (Array.isArray(value) ? value : [value]).map(item => ({ pid: item.ProcessId, parent: item.ParentProcessId, command: item.CommandLine ?? "" })); }
    catch { throw processFailure("runtime_process_inspection_failed"); }
  }
  const result = spawnSync("/bin/ps", ["-u", String(process.getuid()), "-o", "pid=,ppid=,command="], { encoding: "utf8", timeout: 3000, maxBuffer: 4 * 1024 * 1024 });
  if (result.status !== 0) throw processFailure("runtime_process_inspection_failed");
  return result.stdout.split("\n").flatMap(line => {
    const match = /^\s*(\d+)\s+(\d+)\s+(.+)$/u.exec(line);
    return match ? [{ pid: Number(match[1]), parent: Number(match[2]), command: match[3] }] : [];
  });
}

function runtimeExecutablesInUse(executables) {
  const entries = processInventory({ executables });
  return new Set(executables.filter(executable => entries.some(entry =>
    entry.command.startsWith(executable + " ") || entry.command.startsWith('"' + executable + '" '))));
}

/**
 * Processes of one Runtime interpreter with the given roles, identified by their command
 * line at this moment -- never by a saved process id (v2/11 section 1.3).
 */
function runtimeProcessesByRole({ python, roles, birthOf, inventory = processInventory }) {
  const executable = resolve(python);
  return inventory({ executables: [executable] }).flatMap((entry) => {
    const role = classifyRuntimeCommand(executable, entry.command);
    if (!roles.includes(role) || !Number.isSafeInteger(entry.pid) || entry.pid <= 0 || entry.pid === process.pid) return [];
    const birth = birthOf(entry.pid);
    return birth ? [{ pid: entry.pid, birth, role }] : [];
  });
}

async function stopAuthorizedProcesses(processes, { birthOf, signal = (pid, kind) => process.kill(pid, kind),
  exists = pid => { try { process.kill(pid, 0); return true; } catch (error) { if (error.code === "ESRCH") return false; throw error; } },
  wait = milliseconds => new Promise(accept => setTimeout(accept, milliseconds)), gracefulMs = 5000, forcedMs = 2000 } = {}) {
  const remains = item => {
    if (!exists(item.pid)) return false;
    const birth = birthOf(item.pid);
    if (!birth) {
      if (!exists(item.pid)) return false;
      throw processFailure("runtime_process_identity_unverified");
    }
    return birth === item.birth;
  };
  // Signal children before services, then wait for the whole authorized set.
  const selected = [...processes].sort((a, b) => (a.role === "host") - (b.role === "host"));
  for (const item of selected) if (remains(item)) {
    try { signal(item.pid, "SIGTERM"); } catch (error) { if (error.code !== "ESRCH") throw processFailure("runtime_process_stop_failed"); }
  }
  let deadline = Date.now() + gracefulMs;
  while (selected.some(remains) && Date.now() < deadline) await wait(50);
  for (const item of selected) if (remains(item)) {
    try { signal(item.pid, "SIGKILL"); } catch (error) { if (error.code !== "ESRCH") throw processFailure("runtime_process_stop_failed"); }
  }
  deadline = Date.now() + forcedMs;
  while (selected.some(remains) && Date.now() < deadline) await wait(50);
  if (selected.some(remains)) throw processFailure("runtime_process_stop_failed");
}
return { runtimeProcessesByRole, stopAuthorizedProcesses, runtimeExecutablesInUse };
})();
import * as __edgepilot_services_dependency_0 from "node:child_process";
import * as __edgepilot_services_dependency_1 from "node:crypto";
import * as __edgepilot_services_dependency_2 from "node:fs";
import * as __edgepilot_services_dependency_3 from "node:os";
import * as __edgepilot_services_dependency_4 from "node:path";
const { ServiceManager, capturedEnvironment, launcherPath, readServicesRecord, writeLauncher, writeServicesRecord } = (() => {
// User-level services: launcher, service definitions and the service manager
// (docs/architecture/v2/11 section 1; decisions D40, D41).
//
// The service definitions point at a fixed launcher under the Runtime home. The
// launcher carries the current Runtime's Python, arguments and environment and is
// rewritten atomically whenever they change, so an upgrade never rewrites a service
// definition. The launcher is also the only place that defines the environment of the
// Host, the trading service and bootstrap's own trading service commands: the trading
// socket lives under XDG_RUNTIME_DIR/TMPDIR, so all of them must agree.
const { spawn, spawnSync } = __edgepilot_services_dependency_0;
const { randomUUID } = __edgepilot_services_dependency_1;
const { chmodSync, closeSync, constants, existsSync, lstatSync, mkdirSync, openSync, readFileSync, renameSync, rmSync, writeFileSync } = __edgepilot_services_dependency_2;
const { homedir } = __edgepilot_services_dependency_3;
const { dirname, join } = __edgepilot_services_dependency_4;

const SERVICES_SCHEMA = "edgepilot-services-v1";
const MANAGED_MODES = { darwin: "launchd", linux: "systemd", win32: "schtasks" };
// Environment the services keep from the installing session; everything else is dropped.
// The Host passes these on when it runs bootstrap, which rewrites the launcher from them.
const CAPTURED = ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "EDGEPILOT_PROXY_URL", "EDGEPILOT_PROXY_MODE",
  "XDG_RUNTIME_DIR", "TMPDIR", "LANG", "LC_ALL", "EDGEPILOT_LIVE_DASHBOARD_PORT", "EDGEPILOT_RESEARCH_DASHBOARD_PORT",
  "EDGEPILOT_SERVICE_MODE"];
const LAUNCHD_PREFIX = "ai.edgepilot.";

function failure(code) { return Object.assign(new Error(code), { code }); }

function serviceNames(product, environment) {
  const prefix = environment === "local" ? "edgepilot-local-" : "edgepilot-";
  return { host: `${prefix}host-${product}`, tradingd: product === "live" ? `${prefix}tradingd` : null };
}

function launcherPath(home, platform = process.platform) {
  return join(home, "bin", platform === "win32" ? "edgepilot-launch.vbs" : "edgepilot-launch");
}

/** The session environment a launcher pins (proxy, locale and the socket directory roots). */
function capturedEnvironment(source = process.env) {
  const result = {};
  for (const [key, value] of Object.entries(source)) {
    const name = key.toUpperCase();
    if (CAPTURED.includes(name) && typeof value === "string" && value && !/[\0\r\n]/u.test(value)) result[name] = value;
  }
  const noProxy = (result.NO_PROXY ?? "").split(",").map((item) => item.trim()).filter(Boolean);
  if (!noProxy.some((item) => item.toLowerCase() === "127.0.0.1")) result.NO_PROXY = ["127.0.0.1", ...noProxy].join(",");
  return result;
}

const shellQuote = (value) => `'${String(value).replaceAll("'", "'\\''")}'`;
const vbsString = (value) => `"${String(value).replaceAll('"', '""')}"`;
const windowsArgument = (value) => /[\s"]/u.test(value) ? `"${String(value).replaceAll('"', '\\"')}"` : String(value);

/**
 * spec: { runtimeId, python, host: [arguments after python], tradingd: { stateRoot, environment } | null,
 *         environment: captured variables }
 */
function renderLauncher(spec, platform = process.platform) {
  const base = { PYTHONDONTWRITEBYTECODE: "1", PYTHONNOUSERSITE: "1", PYTHONUTF8: "1", ...spec.environment };
  const tradingd = spec.tradingd === null ? null : ["-I", "-B", "-m", "edgepilot.tradingd", "--environment", spec.tradingd.environment];
  const tradingdEnvironment = spec.tradingd === null ? {} : { EDGEPILOT_HOME: spec.tradingd.stateRoot, EDGEPILOT_RUNTIME_ID: spec.runtimeId };
  if (platform === "win32") {
    // The interpreter is always quoted: CreateProcess splits an unquoted path at spaces.
    const command = (args) => vbsString([`"${spec.python}"`, ...args.map(windowsArgument)].join(" "));
    const set = (values) => Object.entries(values).map(([key, value]) => `env(${vbsString(key)}) = ${vbsString(value)}`);
    return [
      `' Generated by EdgePilot bootstrap for Runtime ${spec.runtimeId}; rewritten when the Runtime changes.`,
      "' Runs the Host or the trading service hidden; reruns it after a non-zero exit (D40).",
      "Option Explicit",
      "Dim shell, env, command, code",
      "Set shell = CreateObject(\"WScript.Shell\")",
      "Set env = shell.Environment(\"Process\")",
      ...set(base),
      "If WScript.Arguments.Count < 1 Then WScript.Quit 2",
      "Select Case WScript.Arguments(0)",
      `  Case "host": command = ${command(["-I", "-B", "-m", "edgepilot_runtime_host.host_main", ...spec.host])}`,
      ...(tradingd === null ? [] : [`  Case "tradingd"`, ...set(tradingdEnvironment).map((line) => `    ${line}`), `    command = ${command(tradingd)}`]),
      "  Case Else: WScript.Quit 2",
      "End Select",
      "Do",
      "  code = shell.Run(command, 0, True)",
      "  If code = 0 Then Exit Do",
      "  WScript.Sleep 60000",
      "Loop",
      "WScript.Quit code",
      "",
    ].join("\r\n");
  }
  const exports = (values) => Object.entries(values).map(([key, value]) => `export ${key}=${shellQuote(value)}`);
  const unset = CAPTURED.filter((name) => !(name in base));
  return [
    "#!/bin/sh",
    `# Generated by EdgePilot bootstrap for Runtime ${spec.runtimeId}; rewritten when the Runtime changes.`,
    ...(unset.length ? [`unset ${unset.join(" ")}`] : []),
    ...exports(base),
    "command=\"$1\"",
    "[ $# -gt 0 ] && shift",
    "case \"$command\" in",
    `  host) exec ${[spec.python, "-I", "-B", "-m", "edgepilot_runtime_host.host_main", ...spec.host].map(shellQuote).join(" ")} "$@" ;;`,
    ...(tradingd === null ? [] : [
      `  tradingd) ${exports(tradingdEnvironment).join("; ")}; exec ${[spec.python, ...tradingd].map(shellQuote).join(" ")} "$@" ;;`]),
    "  *) echo 'usage: edgepilot-launch host|tradingd' >&2; exit 2 ;;",
    "esac",
    "",
  ].join("\n");
}

function atomicWrite(path, content, mode) {
  mkdirSync(dirname(path), { recursive: true, mode: 0o700 });
  if (existsSync(path) && lstatSync(path).isSymbolicLink()) throw failure("service_state_invalid");
  const temporary = join(dirname(path), `.${randomUUID()}.tmp`);
  try {
    writeFileSync(temporary, content, { flag: "wx", mode });
    if (process.platform !== "win32") chmodSync(temporary, mode);
    renameSync(temporary, path);
  } finally { rmSync(temporary, { force: true }); }
}

function currentContent(path, encoding) {
  try { return lstatSync(path).isFile() ? readFileSync(path, encoding) : null; } catch { return null; }
}

/** Write the launcher when its content changed; returns whether it did. */
function writeLauncher(home, spec, platform = process.platform) {
  const path = launcherPath(home, platform);
  const content = renderLauncher(spec, platform);
  // wscript reads UTF-16LE with a byte order mark, so user paths may be non-ASCII.
  const bytes = platform === "win32" ? Buffer.concat([Buffer.from([0xff, 0xfe]), Buffer.from(content, "utf16le")]) : Buffer.from(content, "utf8");
  const current = currentContent(path, null);
  if (current !== null && Buffer.compare(current, bytes) === 0) return false;
  atomicWrite(path, bytes, 0o700);
  return true;
}

const xml = (value) => String(value).replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;");
const systemdQuote = (value) => `"${String(value).replaceAll("\\", "\\\\").replaceAll('"', '\\"').replaceAll("%", "%%").replaceAll("$", "$$$$")}"`;

/** The service definition file for one service: { path, content (string), encoding }. */
function renderDefinition({ platform, home, name, kind, product, environment, launcher, user = null, configHome = homedir() }) {
  const description = `EdgePilot ${kind === "host" ? `${product} Host` : "trading service"} (${environment})`;
  const log = join(home, "runtime", "logs", `${kind}.log`);
  if (platform === "darwin") {
    const argumentsXml = ["/bin/sh", launcher, kind].map((item) => `    <string>${xml(item)}</string>`).join("\n");
    return { path: join(configHome, "Library", "LaunchAgents", `${LAUNCHD_PREFIX}${name}.plist`), encoding: "utf8", content: [
      '<?xml version="1.0" encoding="UTF-8"?>',
      '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">',
      '<plist version="1.0">', "<dict>",
      `  <key>Label</key><string>${xml(LAUNCHD_PREFIX + name)}</string>`,
      "  <key>ProgramArguments</key>", "  <array>", argumentsXml, "  </array>",
      "  <key>RunAtLoad</key><true/>",
      "  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>",
      "  <key>ProcessType</key><string>Interactive</string>",
      // Engines must survive a supervisor exit or crash (07 section 2).
      ...(kind === "tradingd" ? ["  <key>AbandonProcessGroup</key><true/>"] : []),
      `  <key>StandardOutPath</key><string>${xml(log)}</string>`,
      `  <key>StandardErrorPath</key><string>${xml(log)}</string>`,
      "</dict>", "</plist>", "",
    ].join("\n") };
  }
  if (platform === "linux") {
    return { path: join(configHome, ".config", "systemd", "user", `${name}.service`), encoding: "utf8", content: [
      "[Unit]", `Description=${description}`, "",
      "[Service]", "Type=simple",
      `ExecStart=/bin/sh ${systemdQuote(launcher)} ${kind}`,
      `StandardOutput=append:${log}`, "StandardError=inherit",
      "Restart=on-failure", "RestartSec=2",
      // KillMode=process keeps the engines; an explicit stop drains them first (11 section 1.1).
      ...(kind === "tradingd" ? ["KillMode=process", `ExecStop=/bin/sh ${systemdQuote(launcher)} tradingd --drain`, "TimeoutStopSec=180"] : []),
      "", "[Install]", "WantedBy=default.target", "",
    ].join("\n") };
  }
  if (platform === "win32") {
    const wscript = join(process.env.SYSTEMROOT ?? "C:\\Windows", "System32", "wscript.exe");
    return { path: join(home, "runtime", "services", `${name}.xml`), encoding: "utf16le", content: [
      '<?xml version="1.0" encoding="UTF-16"?>',
      '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">',
      `  <RegistrationInfo><Description>${xml(description)}</Description></RegistrationInfo>`,
      `  <Triggers><LogonTrigger><Enabled>true</Enabled><UserId>${xml(user)}</UserId></LogonTrigger></Triggers>`,
      `  <Principals><Principal id="Author"><UserId>${xml(user)}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>`,
      "  <Settings>",
      "    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>",
      "    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>",
      "    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>",
      "    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>",
      "    <Hidden>true</Hidden>",
      "    <StartWhenAvailable>true</StartWhenAvailable>",
      "    <RestartOnFailure><Interval>PT1M</Interval><Count>999</Count></RestartOnFailure>",
      "  </Settings>",
      `  <Actions Context="Author"><Exec><Command>${xml(wscript)}</Command><Arguments>${xml(`//B //Nologo "${launcher}" ${kind}`)}</Arguments></Exec></Actions>`,
      "</Task>", "",
    ].join("\r\n") };
  }
  throw failure("platform_unsupported");
}

// Absolute paths: bootstrap runs with a minimal environment that may have no PATH.
function systemCommand(command) {
  if (command === "launchctl") return "/bin/launchctl";
  if (command === "systemctl") return ["/usr/bin/systemctl", "/bin/systemctl"].find((path) => existsSync(path)) ?? "/usr/bin/systemctl";
  if (command === "schtasks") return join(process.env.SYSTEMROOT ?? "C:\\Windows", "System32", "schtasks.exe");
  throw failure("service_command_invalid");
}

function defaultRun(command, args) {
  const result = spawnSync(systemCommand(command), args, { encoding: "utf8", timeout: 60_000, windowsHide: true });
  return { status: result.error ? null : result.status, stdout: result.stdout ?? "", stderr: result.stderr ?? "" };
}

/**
 * One product's services under one service manager. ``run(command, args)`` is injected in
 * tests; the default runs the real manager and is never used by tests (D41).
 */
class ServiceManager {
  constructor({ home, product, environment, platform = process.platform, run = defaultRun, configHome = homedir(),
    uid = typeof process.getuid === "function" ? process.getuid() : null,
    user = process.env.USERDOMAIN && process.env.USERNAME ? `${process.env.USERDOMAIN}\\${process.env.USERNAME}` : process.env.USERNAME ?? null,
    spawnDetached = defaultSpawnDetached, managed = process.env.EDGEPILOT_SERVICE_MODE !== "detached" }) {
    // EDGEPILOT_SERVICE_MODE=detached: never touch the service manager (tests, managed desktops).
    Object.assign(this, { home, product, environment, platform, run, configHome, uid, user, spawnDetached, managed });
    this.names = serviceNames(product, environment);
    this.launcher = launcherPath(home, platform);
    this.mode = null;
  }

  kinds() { return this.names.tradingd === null ? ["host"] : ["host", "tradingd"]; }

  #definition(kind) {
    return renderDefinition({ platform: this.platform, home: this.home, name: this.names[kind], kind, product: this.product,
      environment: this.environment, launcher: this.launcher, user: this.user, configHome: this.configHome });
  }

  #label(kind) { return `gui/${this.uid}/${LAUNCHD_PREFIX}${this.names[kind]}`; }
  #task(kind) { return `EdgePilot\\${this.names[kind]}`; }
  #ok(command, args) { return this.run(command, args).status === 0; }

  /** Whether the manager has the service loaded (it may or may not run right now). */
  registered(kind) {
    if (!this.managed) return false;
    if (this.platform === "darwin") return this.#ok("launchctl", ["print", this.#label(kind)]);
    if (this.platform === "linux") return this.#ok("systemctl", ["--user", "is-enabled", "--quiet", `${this.names[kind]}.service`]);
    return this.#ok("schtasks", ["/Query", "/TN", this.#task(kind)]);
  }

  /** Whether the service's process currently runs according to the manager. */
  active(kind) {
    if (!this.managed) return false;
    if (this.platform === "darwin") return /\bstate = running\b/u.test(this.run("launchctl", ["print", this.#label(kind)]).stdout);
    if (this.platform === "linux") return this.#ok("systemctl", ["--user", "is-active", "--quiet", `${this.names[kind]}.service`]);
    return /"Running"/u.test(this.run("schtasks", ["/Query", "/TN", this.#task(kind), "/FO", "CSV", "/NH"]).stdout);
  }

  /**
   * Write the definition and load it when it is new or changed. A loaded trading
   * service is never reloaded here: unloading it would drain every engine. Returns
   * false when the manager is unavailable (the caller falls back to detached starts).
   */
  register(kind, { reloadLoaded = kind !== "tradingd" } = {}) {
    const definition = this.#definition(kind);
    const bytes = definition.encoding === "utf16le"
      ? Buffer.concat([Buffer.from([0xff, 0xfe]), Buffer.from(definition.content, "utf16le")]) : Buffer.from(definition.content, "utf8");
    const current = currentContent(definition.path, null);
    const changed = current === null || Buffer.compare(current, bytes) !== 0;
    const loaded = this.registered(kind);
    if (loaded && (!changed || !reloadLoaded)) return true;
    try { atomicWrite(definition.path, bytes, 0o600); } catch { return false; }
    if (this.platform === "darwin") {
      if (loaded) this.run("launchctl", ["bootout", this.#label(kind)]);
      return this.#ok("launchctl", ["bootstrap", `gui/${this.uid}`, definition.path]);
    }
    if (this.platform === "linux") {
      return this.#ok("systemctl", ["--user", "daemon-reload"]) && this.#ok("systemctl", ["--user", "enable", `${this.names[kind]}.service`]);
    }
    return this.#ok("schtasks", ["/Create", "/TN", this.#task(kind), "/XML", definition.path, "/F"]);
  }

  /** Start through the manager (a no-op when it already runs). */
  start(kind) {
    if (this.platform === "darwin") return this.#ok("launchctl", ["kickstart", this.#label(kind)]);
    if (this.platform === "linux") return this.#ok("systemctl", ["--user", "start", `${this.names[kind]}.service`]);
    return this.#ok("schtasks", ["/Run", "/TN", this.#task(kind)]);
  }

  /**
   * Stop through the manager without it restarting the process. Only for a process that
   * did not stop through its own protocol (Host ``/host/stop``, trading service drain).
   */
  stop(kind) {
    if (!this.managed) return;
    if (this.platform === "darwin") this.run("launchctl", ["bootout", this.#label(kind)]);
    else if (this.platform === "linux") this.run("systemctl", ["--user", "stop", `${this.names[kind]}.service`]);
    else this.run("schtasks", ["/End", "/TN", this.#task(kind)]);
  }

  /** Remove the definition. The caller has already stopped the service through its own protocol. */
  unregister(kind) {
    if (!this.managed) return;
    const definition = this.#definition(kind);
    if (this.platform === "darwin") this.run("launchctl", ["bootout", this.#label(kind)]);
    else if (this.platform === "linux") this.run("systemctl", ["--user", "disable", `${this.names[kind]}.service`]);
    else this.run("schtasks", ["/Delete", "/TN", this.#task(kind), "/F"]);
    rmSync(definition.path, { force: true });
    if (this.platform === "linux") this.run("systemctl", ["--user", "daemon-reload"]);
  }

  /**
   * Make sure ``kind`` is registered and started. Falls back to starting the launcher
   * detached (no autostart, no crash restart) when the manager is unavailable.
   */
  ensureRunning(kind) {
    if (!(this.platform in MANAGED_MODES)) throw failure("platform_unsupported");
    if (this.managed && this.register(kind) && this.start(kind)) {
      this.mode = MANAGED_MODES[this.platform];
      return this.mode;
    }
    this.spawnDetached({ platform: this.platform, launcher: this.launcher, kind, log: join(this.home, "runtime", "logs", `${kind}.log`) });
    this.mode = "detached";
    return this.mode;
  }
}

function defaultSpawnDetached({ platform, launcher, kind, log }) {
  mkdirSync(dirname(log), { recursive: true, mode: 0o700 });
  const output = openSync(log, constants.O_WRONLY | constants.O_CREAT | constants.O_APPEND, 0o600);
  try {
    const [command, args] = platform === "win32"
      ? [join(process.env.SYSTEMROOT ?? "C:\\Windows", "System32", "wscript.exe"), ["//B", "//Nologo", launcher, kind]]
      : ["/bin/sh", [launcher, kind]];
    const child = spawn(command, args, { detached: true, stdio: ["ignore", output, output], windowsHide: true });
    child.unref();
  } finally { closeSync(output); }
}

/** ``runtime/services.json``: how the services run and the commands the Host may run (D43). */
function writeServicesRecord(home, value) {
  atomicWrite(join(home, "runtime", "services.json"), `${JSON.stringify({ schema: SERVICES_SCHEMA, ...value }, null, 2)}\n`, 0o600);
}

function readServicesRecord(home) {
  const path = join(home, "runtime", "services.json");
  try {
    if (!lstatSync(path).isFile() || lstatSync(path).size > 64 * 1024) return null;
    const value = JSON.parse(readFileSync(path, "utf8"));
    return value?.schema === SERVICES_SCHEMA ? value : null;
  } catch { return null; }
}
return { ServiceManager, capturedEnvironment, launcherPath, readServicesRecord, writeLauncher, writeServicesRecord };
})();

const MANIFEST_DOMAIN = Buffer.from("EdgePilot Runtime Manifest V1\0", "utf8");
const CHANNEL_DOMAIN = Buffer.from("EdgePilot Runtime Channel V1\0", "utf8");
const SPKI_ED25519_PREFIX = Buffer.from("302a300506032b6570032100", "hex");
const MAX_MANIFEST_BYTES = 4 * 1024 * 1024;
const MAX_CHANNEL_BYTES = 512 * 1024;
const MAX_FILES = 200_000;
const METADATA_DOWNLOAD_TIMEOUT_MS = 120_000;
const RUNTIME_DOWNLOAD_TIMEOUT_MS = 15 * 60_000;
export const RUNTIME_PROBE_TIMEOUT_MS = 300_000;
// Windows cold starts re-hash the full installed Runtime tree before the Host
// writes its connection; 60s was below measured verify_tree cost (~66s).
export const HOST_START_TIMEOUT_MS = 90_000;
const DEFAULT_HOST_PORT = 0;
const BOOTSTRAP_PRODUCT_VERSION = "1.3.14";
const BOOTSTRAP_COMPATIBILITY_VERSION = "1.0.0";
const SUPPORTED_CONTRACT_VERSION = "1.0.0";
const PRODUCTION_MARKETPLACE_ORIGIN = "https://api.edgepilotai.io";
const LOCAL_MARKETPLACE_ORIGIN = "http://127.0.0.1:18080";
// Finder/Explorer may materialize these files while a Runtime state directory
// is being inspected. They are not Runtime releases and must not make a local
// build fail; unknown entries remain fail-closed below.
const GENERATED_FILESYSTEM_METADATA = new Set([".DS_Store", "Thumbs.db", "desktop.ini"]);
const ENVIRONMENT_PORTS = Object.freeze({
  local: Object.freeze({ live: 18787, research: 18686 }),
  production: Object.freeze({ live: 8787, research: 8686 }),
});

export class BootstrapError extends Error {
  constructor(code, message) {
    super(message);
    this.code = code;
  }
}

export function validateEnvironmentIsolation({ product, environmentName, marketplaceOrigin, runtimeHome, liveStateRoot, researchStateRoot, liveDashboardPort, researchDashboardPort }) {
  const ports = ENVIRONMENT_PORTS[environmentName];
  if (ports === undefined) fail("environment_invalid", "Runtime environment is invalid");
  if (liveDashboardPort !== ports.live || researchDashboardPort !== ports.research) {
    fail("environment_port_mismatch", `${environmentName} requires Dashboard ports ${ports.live}/${ports.research}`);
  }
  if (product === "live") {
    const expectedOrigin = environmentName === "local" ? LOCAL_MARKETPLACE_ORIGIN : PRODUCTION_MARKETPLACE_ORIGIN;
    if (marketplaceOrigin !== expectedOrigin) {
      fail("environment_origin_mismatch", `${environmentName} Live Runtime requires its fixed Marketplace origin`);
    }
  } else if (marketplaceOrigin !== null) {
    fail("environment_origin_mismatch", "Research Runtime must not receive a Marketplace identity origin");
  }
  if (environmentName === "local") {
    const forbidden = new Set([
      physicalStatePath(join(homedir(), ".edgepilot-runtime-live")),
      physicalStatePath(join(homedir(), ".edgepilot-runtime-research")),
      physicalStatePath(join(homedir(), ".edgepilot-runtime-live-production")),
      physicalStatePath(join(homedir(), ".edgepilot-runtime-research-production")),
      physicalStatePath(join(homedir(), ".edgepilot")),
      physicalStatePath(join(homedir(), ".edgepilot-research")),
    ]);
    if ([runtimeHome, liveStateRoot, researchStateRoot].some((value) => {
      const candidate = physicalStatePath(value);
      return [...forbidden].some((root) => candidate === root || candidate.startsWith(`${root}${sep}`));
    })) {
      fail("environment_state_mismatch", "local Runtime requires isolated Runtime and product state roots");
    }
  }
}

// MAX_PATH is 260 WCHARs including the terminator. LoadLibrary still applies it
// to extension modules with embedded manifests even when long paths are enabled,
// so the budget is measured against the real file inventory, not a guess.
const WINDOWS_MAX_PATH_CHARS = 259;

export function validateWindowsReleasePathBudget(releaseRoot, manifest, platform = process.platform) {
  if (platform !== "win32") return;
  let deepest = 0;
  for (const entry of manifest.payload.files) if (entry.path.length > deepest) deepest = entry.path.length;
  const longest = releaseRoot.length + 1 + deepest;
  if (longest > WINDOWS_MAX_PATH_CHARS) {
    fail("runtime_path_too_long", `Windows Runtime path would reach ${longest} characters; choose a --runtime-home at least ${longest - WINDOWS_MAX_PATH_CHARS} characters shorter`);
  }
}

function physicalStatePath(value) {
  let ancestor = resolve(value);
  const missing = [];
  while (!existsSync(ancestor)) {
    if (dirname(ancestor) === ancestor) fail("environment_state_mismatch", "State path cannot be resolved");
    missing.unshift(basename(ancestor));
    ancestor = dirname(ancestor);
  }
  const physical = resolve(realpathSync(ancestor), ...missing);
  return process.platform === "win32" ? physical.toLowerCase() : physical;
}

export function canonicalBytes(value) {
  return Buffer.from(canonical(value), "utf8");
}

function canonical(value) {
  if (value === null || typeof value === "boolean" || typeof value === "string") {
    return JSON.stringify(value);
  }
  if (typeof value === "number") {
    if (!Number.isSafeInteger(value)) fail("manifest_number_invalid", "manifest numbers must be safe integers");
    return String(value);
  }
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  if (typeof value === "object") {
    const keys = Object.keys(value).sort(compareCodePoints);
    return `{${keys.map((key) => `${JSON.stringify(key)}:${canonical(value[key])}`).join(",")}}`;
  }
  fail("manifest_json_invalid", "manifest contains a non-JSON value");
}

function compareCodePoints(left, right) {
  const a = Array.from(left, (value) => value.codePointAt(0));
  const b = Array.from(right, (value) => value.codePointAt(0));
  for (let index = 0; index < Math.min(a.length, b.length); index += 1) {
    if (a[index] !== b[index]) return a[index] - b[index];
  }
  return a.length - b.length;
}

function sha256(data) {
  return `sha256:${createHash("sha256").update(data).digest("hex")}`;
}

function exactKeys(value, required, code) {
  if (value === null || typeof value !== "object" || Array.isArray(value)) fail(code, "expected an object");
  const actual = Object.keys(value).sort();
  const expected = [...required].sort();
  if (actual.length !== expected.length || actual.some((key, index) => key !== expected[index])) {
    fail(code, "object fields differ from the signed contract");
  }
  return value;
}

function validateRelativePath(value, targetOs) {
  if (typeof value !== "string" || value.length < 1 || value.length > 1024 || value.includes("\\") || value.startsWith("/")) {
    fail("runtime_path_invalid", "Runtime path is not normalized");
  }
  const parts = value.split("/");
  if (!/^[\x20-\x7e]+$/u.test(value)) fail("runtime_path_invalid", "bootstrap Runtime paths must be ASCII");
  if (parts.some((part) => !part || part === "." || part === ".." || /[\u0000-\u001f\u007f]/u.test(part))) {
    fail("runtime_path_invalid", "Runtime path contains an unsafe segment");
  }
  if (targetOs === "windows") {
    const reserved = /^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?$/iu;
    if (parts.some((part) => reserved.test(part) || /[. ]$/u.test(part) || /[:*?"<>|]/u.test(part))) {
      fail("runtime_path_invalid", "Runtime path is not portable to Windows");
    }
  }
}

function currentTarget() {
  const os = { darwin: "macos", win32: "windows", linux: "linux" }[hostPlatform()];
  const architecture = { arm64: "arm64", x64: "amd64" }[hostArch()];
  if (!os || !architecture) fail("platform_unsupported", "this operating system or CPU is unsupported");
  const glibc = os === "linux" ? process.report?.getReport?.().header?.glibcVersionRuntime ?? null : null;
  return { os, arch: architecture, osVersion: hostRelease(), glibc };
}

export function validateManifest(value, trustedKeys, { enforcePlatform = true } = {}) {
  const envelope = exactKeys(value, ["runtime_id", "payload", "signatures"], "manifest_invalid");
  const payload = exactKeys(
    envelope.payload,
    [
      "schema",
      "contract_version",
      "release_version",
      "symlink_policy",
      "archive_size",
      "archive_sha256",
      "target",
      "python",
      "profiles",
      "operation_registry_digest",
      "components",
      "capabilities",
      "files",
      "sbom",
    ],
    "manifest_payload_invalid",
  );
  if (payload.schema !== "edgepilot-runtime-manifest-v1" || payload.contract_version?.major !== 1 || payload.symlink_policy !== "dereference_and_forbid") {
    fail("manifest_version_unsupported", "Runtime Manifest version or policy is unsupported");
  }
  if (!Number.isSafeInteger(payload.archive_size) || payload.archive_size < 1 || !isDigest(payload.archive_sha256)) {
    fail("archive_identity_invalid", "Runtime archive identity is invalid");
  }
  if (envelope.runtime_id !== sha256(canonicalBytes(payload))) fail("runtime_identity_invalid", "Runtime ID differs from canonical payload");
  const target = exactKeys(
    payload.target,
    ["os", "arch", "minimum_os_version", "libc_family", "minimum_libc_version", "archive_format"],
    "target_invalid",
  );
  if (target.archive_format !== "zip") fail("archive_format_unsupported", "bootstrap currently accepts ZIP Runtime archives");
  if (enforcePlatform) {
    const host = currentTarget();
    if (target.os !== host.os || target.arch !== host.arch) fail("platform_mismatch", "Runtime target differs from this machine");
    if (target.os === "linux") {
      if (target.libc_family !== "glibc" || typeof target.minimum_libc_version !== "string" || typeof host.glibc !== "string" || compareNumericVersion(host.glibc, target.minimum_libc_version) < 0) fail("platform_mismatch", "Runtime requires a newer glibc platform");
    }
  }
  const python = exactKeys(
    payload.python,
    ["implementation", "version", "abi", "executable", "isolated", "user_site_enabled"],
    "python_identity_invalid",
  );
  const pythonVersion = typeof python.version === "string" ? /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/u.exec(python.version) : null;
  if (python.implementation !== "cpython" || pythonVersion === null || compareNumericVersion(python.version, "3.12.0") < 0 || python.abi !== `cp${pythonVersion[1]}${pythonVersion[2]}` || python.isolated !== true || python.user_site_enabled !== false) {
    fail("python_identity_invalid", "Runtime Python identity is unsupported");
  }
  if (!Array.isArray(payload.files) || payload.files.length < 1 || payload.files.length > MAX_FILES) {
    fail("runtime_inventory_invalid", "Runtime file inventory is invalid");
  }
  const paths = new Set();
  const portable = new Set();
  for (const entry of payload.files) {
    exactKeys(entry, ["path", "kind", "size", "sha256", "executable"], "runtime_inventory_invalid");
    validateRelativePath(entry.path, target.os);
    if (entry.kind !== "file" || !Number.isSafeInteger(entry.size) || entry.size < 0 || !isDigest(entry.sha256) || typeof entry.executable !== "boolean") {
      fail("runtime_inventory_invalid", "Runtime file entry is invalid");
    }
    if (paths.has(entry.path)) fail("runtime_path_collision", "Runtime contains duplicate paths");
    paths.add(entry.path);
    const key = ["windows", "macos"].includes(target.os) ? entry.path.normalize("NFC").toLocaleLowerCase("en-US") : entry.path;
    if (portable.has(key)) fail("runtime_path_collision", "Runtime paths collide on the target platform");
    portable.add(key);
  }
  validateRelativePath(python.executable, target.os);
  const pythonEntry = payload.files.find((entry) => entry.path === python.executable);
  if (!pythonEntry?.executable) fail("python_identity_invalid", "bundled Python executable is missing");
  if (trustedKeys === null) return envelope;
  if (!Array.isArray(envelope.signatures) || envelope.signatures.length < 1) fail("signature_missing", "Runtime manifest has no signature");
  const preimage = Buffer.concat([MANIFEST_DOMAIN, canonicalBytes(payload)]);
  let foundTrusted = false;
  for (const item of envelope.signatures) {
    exactKeys(item, ["algorithm", "key_id", "signature_base64"], "signature_invalid");
    const rawKey = trustedKeys.get(item.key_id);
    if (!rawKey) continue;
    foundTrusted = true;
    if (item.algorithm !== "ed25519") continue;
    const signature = strictBase64(item.signature_base64, 64, "signature_invalid");
    const key = createPublicKey({ key: Buffer.concat([SPKI_ED25519_PREFIX, rawKey]), format: "der", type: "spki" });
    if (verifySignature(null, preimage, key, signature)) return envelope;
  }
  if (!foundTrusted) fail("untrusted_key", "Runtime manifest has no trusted signer");
  fail("signature_invalid", "Runtime manifest signature is invalid");
}

export function validateChannel(value, trustedKeys, channelUrl, {
  enforcePlatform = true,
  requestedChannel = null,
  runtimePin = null,
  pluginVersion = null,
} = {}) {
  const envelope = exactKeys(value, ["channel_id", "payload", "signatures"], "channel_invalid");
  const payload = exactKeys(envelope.payload, ["schema", "version", "channel", "published_at", "targets"], "channel_payload_invalid");
  if (payload.schema !== "edgepilot-runtime-channel-v1" || payload.version !== 1) fail("channel_version_unsupported", "Runtime channel version is unsupported");
  if (!["local", "production"].includes(payload.channel) || (requestedChannel !== null && payload.channel !== requestedChannel)) fail("channel_mismatch", "Runtime channel differs from the configured environment");
  if (typeof payload.published_at !== "string" || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/u.test(payload.published_at)) fail("channel_invalid", "Runtime channel publication time is invalid");
  if (envelope.channel_id !== sha256(canonicalBytes(payload))) fail("channel_identity_invalid", "Runtime channel ID differs from canonical payload");
  verifyEnvelopeSignatures(envelope.signatures, trustedKeys, Buffer.concat([CHANNEL_DOMAIN, canonicalBytes(payload)]), "Channel");
  if (!Array.isArray(payload.targets) || payload.targets.length < 1 || payload.targets.length > 64) fail("channel_targets_invalid", "Runtime channel targets are invalid");
  const host = enforcePlatform ? currentTarget() : null;
  let selected = null;
  const identities = new Set();
  for (const raw of payload.targets) {
    const target = exactKeys(raw, ["os", "arch", "runtime_id", "manifest_url", "archive_url", "archive_size", "archive_sha256", "minimum_bootstrap_version", "minimum_plugin_version", "minimum_contract_version", "capabilities", "previous_runtime_id", "fallback_runtime_id", "revoked", "disabled", "signing_key_id"], "channel_target_invalid");
    if (!isDigest(target.runtime_id) || !Number.isSafeInteger(target.archive_size) || target.archive_size < 1 || !isDigest(target.archive_sha256)) fail("channel_target_invalid", "Runtime channel artifact identity is invalid");
    if (identities.has(`${target.os}/${target.arch}/${target.runtime_id}`)) fail("channel_target_invalid", "Runtime channel target is duplicated");
    identities.add(`${target.os}/${target.arch}/${target.runtime_id}`);
    for (const name of ["minimum_bootstrap_version", "minimum_plugin_version", "minimum_contract_version"]) if (!isSemver(target[name])) fail("channel_target_invalid", "Runtime channel compatibility version is invalid");
    if ((target.previous_runtime_id !== null && !isDigest(target.previous_runtime_id)) || (target.fallback_runtime_id !== null && !isDigest(target.fallback_runtime_id)) || typeof target.revoked !== "boolean" || typeof target.disabled !== "boolean" || typeof target.signing_key_id !== "string" || target.signing_key_id.length < 1) fail("channel_target_invalid", "Runtime channel recovery metadata is invalid");
    validateArtifactUrl(channelUrl, target.manifest_url);
    validateArtifactUrl(channelUrl, target.archive_url);
    if ((host === null || (target.os === host.os && target.arch === host.arch)) && (runtimePin === null || target.runtime_id === runtimePin)) {
      if (selected !== null) fail("channel_target_ambiguous", "Runtime channel selected more than one target");
      selected = target;
    }
  }
  if (selected === null) fail(runtimePin === null ? "platform_unsupported" : "runtime_pin_unavailable", "Runtime channel has no matching target");
  if (selected.revoked || selected.disabled) fail("runtime_revoked", "selected Runtime is disabled or revoked");
  if (compareSemver(BOOTSTRAP_COMPATIBILITY_VERSION, selected.minimum_bootstrap_version) < 0) fail("bootstrap_incompatible", "bootstrap is older than the selected Runtime requires");
  if (compareSemver(SUPPORTED_CONTRACT_VERSION, selected.minimum_contract_version) < 0) fail("contract_incompatible", "Runtime contract is newer than this bootstrap supports");
  if (pluginVersion !== null && compareSemver(pluginVersion, selected.minimum_plugin_version) < 0) fail("plugin_incompatible", "plugin is older than the selected Runtime requires");
  return { envelope, target: selected };
}

function verifyEnvelopeSignatures(signatures, trustedKeys, preimage, label) {
  if (!Array.isArray(signatures) || signatures.length < 1) fail("signature_missing", `${label} has no signature`);
  let foundTrusted = false;
  for (const item of signatures) {
    exactKeys(item, ["algorithm", "key_id", "signature_base64"], "signature_invalid");
    const rawKey = trustedKeys.get(item.key_id);
    if (!rawKey) continue;
    foundTrusted = true;
    if (item.algorithm !== "ed25519") continue;
    const signature = strictBase64(item.signature_base64, 64, "signature_invalid");
    const key = createPublicKey({ key: Buffer.concat([SPKI_ED25519_PREFIX, rawKey]), format: "der", type: "spki" });
    if (verifySignature(null, preimage, key, signature)) return item.key_id;
  }
  if (!foundTrusted) fail("untrusted_key", `${label} has no trusted signer`);
  fail("signature_invalid", `${label} signature is invalid`);
}

function validateArtifactUrl(channelUrl, value) {
  let channel;
  let artifact;
  try { channel = new URL(channelUrl); artifact = new URL(value); } catch { fail("download_url_invalid", "Runtime channel contains an invalid artifact URL"); }
  if (channel.protocol !== "https:" || artifact.protocol !== "https:" || channel.username || channel.password || artifact.username || artifact.password || channel.origin !== artifact.origin) fail("download_url_invalid", "Runtime channel artifacts must use credential-free HTTPS on the channel origin");
}

function validateFunctionalUrl(value) {
  let parsed;
  try { parsed = new URL(value); } catch { fail("download_url_invalid", "Runtime download URL is invalid"); }
  if (!["http:", "https:"].includes(parsed.protocol) || parsed.username || parsed.password) fail("download_url_invalid", "Runtime download URL must use HTTP(S) without credentials");
  return parsed;
}

export function validateFunctionalChannel(value, channelUrl, { enforcePlatform = true, expectedProduct = null, expectedProductVersion = null } = {}) {
  const channel = exactKeys(value, ["schema", "product", "channel", "bootstrap", "targets"], "channel_invalid");
  if (channel.schema !== "edgepilot-channel-v1" || !["live", "research"].includes(channel.product) || !["local", "production"].includes(channel.channel)) fail("channel_invalid", "functional Runtime channel is invalid");
  if (expectedProduct !== null && channel.product !== expectedProduct) fail("runtime_product_incompatible", "plugin selected another product Runtime channel");
  const bootstrap = exactKeys(channel.bootstrap, ["version", "url", "size", "sha256"], "channel_invalid");
  if (typeof bootstrap.version !== "string" || !Number.isSafeInteger(bootstrap.size) || bootstrap.size < 1 || !isDigest(bootstrap.sha256)) fail("channel_invalid", "Bootstrap artifact is invalid");
  validateFunctionalUrl(channelUrl); validateFunctionalUrl(bootstrap.url);
  if (!Array.isArray(channel.targets) || channel.targets.length < 1) fail("channel_invalid", "Runtime channel has no targets");
  const host = enforcePlatform ? currentTarget() : null;
  let selected = null;
  for (const raw of channel.targets) {
    const target = exactKeys(raw, ["os", "arch", "runtime_id", "release_version", "manifest_url", "archive_url", "archive_size", "archive_sha256"], "channel_invalid");
    if (!["macos", "windows", "linux"].includes(target.os) || !["arm64", "amd64"].includes(target.arch) || typeof target.release_version !== "string" || !isDigest(target.runtime_id) || !Number.isSafeInteger(target.archive_size) || target.archive_size < 1 || !isDigest(target.archive_sha256)) fail("channel_invalid", "Runtime target is invalid");
    validateFunctionalUrl(target.manifest_url); validateFunctionalUrl(target.archive_url);
    if (host === null || (host.os === target.os && host.arch === target.arch)) {
      if (selected !== null) fail("channel_target_ambiguous", "Runtime channel selected more than one target");
      selected = target;
    }
  }
  if (selected === null) {
    const available = channel.targets.map((target) => `${target.os}-${target.arch}`).join(", ");
    const hostLabel = host === null ? "unknown" : `${host.os}-${host.arch}`;
    fail("platform_unsupported", `Runtime channel has no matching target for ${hostLabel}; available targets: ${available}`);
  }
  if (expectedProductVersion !== null && selected.release_version !== expectedProductVersion) fail("runtime_version_incompatible", "plugin requires another Runtime product version");
  return { channel, target: selected };
}

function manifestProduct(manifest) {
  const profiles = manifest?.payload?.profiles;
  if (!Array.isArray(profiles) || profiles.length !== 1 || !["live", "research"].includes(profiles[0])) fail("runtime_product_incompatible", "Runtime must contain exactly one product profile");
  return profiles[0];
}

function isSemver(value) { return typeof value === "string" && /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-[0-9A-Za-z.-]+)?$/u.test(value); }
function compareSemver(left, right) {
  if (!isSemver(left) || !isSemver(right)) fail("version_invalid", "version is not SemVer");
  const a = left.split("-", 1)[0].split(".").map(Number);
  const b = right.split("-", 1)[0].split(".").map(Number);
  for (let index = 0; index < 3; index += 1) if (a[index] !== b[index]) return a[index] < b[index] ? -1 : 1;
  return 0;
}
function compareNumericVersion(left, right) {
  const a = String(left).split(".").map(Number);
  const b = String(right).split(".").map(Number);
  if (a.some((value) => !Number.isSafeInteger(value) || value < 0) || b.some((value) => !Number.isSafeInteger(value) || value < 0)) fail("platform_mismatch", "platform version is invalid");
  for (let index = 0; index < Math.max(a.length, b.length); index += 1) {
    const x = a[index] ?? 0; const y = b[index] ?? 0;
    if (x !== y) return x < y ? -1 : 1;
  }
  return 0;
}

function isDigest(value) {
  return typeof value === "string" && /^sha256:[0-9a-f]{64}$/u.test(value);
}

function strictBase64(value, length, code) {
  if (typeof value !== "string" || !/^[A-Za-z0-9+/]*={0,2}$/u.test(value) || value.length % 4 !== 0) fail(code, "base64 value is invalid");
  const decoded = Buffer.from(value, "base64");
  if (decoded.length !== length || decoded.toString("base64") !== value) fail(code, "base64 value has the wrong length or encoding");
  return decoded;
}

async function fileDigest(path) {
  const digest = createHash("sha256");
  await pipeline(createReadStream(path), new Transform({ transform(chunk, _encoding, callback) { digest.update(chunk); callback(); } }));
  return `sha256:${digest.digest("hex")}`;
}

function readExactly(descriptor, buffer, offset, length, position) {
  let completed = 0;
  while (completed < length) {
    const count = readSync(descriptor, buffer, offset + completed, length - completed, position + completed);
    if (count === 0) fail("archive_invalid", "Runtime ZIP ended unexpectedly");
    completed += count;
  }
}

function parseZipEntries(archivePath, expected) {
  const descriptor = openSync(archivePath, "r");
  try {
    const size = fstatSync(descriptor).size;
    const tailLength = Math.min(size, 65_557);
    const tail = Buffer.alloc(tailLength);
    readExactly(descriptor, tail, 0, tailLength, size - tailLength);
    let eocd = -1;
    for (let index = tail.length - 22; index >= 0; index -= 1) {
      if (tail.readUInt32LE(index) === 0x06054b50) { eocd = index; break; }
    }
    if (eocd < 0) fail("archive_invalid", "Runtime ZIP has no end record");
    const disk = tail.readUInt16LE(eocd + 4);
    const centralDisk = tail.readUInt16LE(eocd + 6);
    const count = tail.readUInt16LE(eocd + 10);
    const centralSize = tail.readUInt32LE(eocd + 12);
    const centralOffset = tail.readUInt32LE(eocd + 16);
    const commentLength = tail.readUInt16LE(eocd + 20);
    if (disk !== 0 || centralDisk !== 0 || count === 0xffff || centralSize === 0xffffffff || centralOffset === 0xffffffff || eocd + 22 + commentLength !== tail.length) {
      fail("archive_zip64_unsupported", "Runtime ZIP central directory is unsupported");
    }
    if (count !== expected.size || centralOffset + centralSize > size) fail("archive_inventory_mismatch", "Runtime ZIP inventory differs");
    const central = Buffer.alloc(centralSize);
    readExactly(descriptor, central, 0, centralSize, centralOffset);
    const entries = [];
    const names = new Set();
    let cursor = 0;
    for (let entryIndex = 0; entryIndex < count; entryIndex += 1) {
      if (cursor + 46 > central.length || central.readUInt32LE(cursor) !== 0x02014b50) fail("archive_invalid", "Runtime ZIP central entry is invalid");
      const flags = central.readUInt16LE(cursor + 8);
      const method = central.readUInt16LE(cursor + 10);
      let compressedSize = central.readUInt32LE(cursor + 20);
      let uncompressedSize = central.readUInt32LE(cursor + 24);
      const nameLength = central.readUInt16LE(cursor + 28);
      const extraLength = central.readUInt16LE(cursor + 30);
      const entryCommentLength = central.readUInt16LE(cursor + 32);
      const externalAttributes = central.readUInt32LE(cursor + 38);
      let localOffset = central.readUInt32LE(cursor + 42);
      const end = cursor + 46 + nameLength + extraLength + entryCommentLength;
      if (end > central.length || (flags & 1) !== 0 || ![0, 8].includes(method)) fail("archive_invalid", "Runtime ZIP entry is encrypted, truncated or unsupported");
      const name = central.subarray(cursor + 46, cursor + 46 + nameLength).toString("utf8");
      const extra = central.subarray(cursor + 46 + nameLength, cursor + 46 + nameLength + extraLength);
      const zip64 = parseZip64(extra, uncompressedSize === 0xffffffff, compressedSize === 0xffffffff, localOffset === 0xffffffff);
      if (uncompressedSize === 0xffffffff) uncompressedSize = zip64.shift();
      if (compressedSize === 0xffffffff) compressedSize = zip64.shift();
      if (localOffset === 0xffffffff) localOffset = zip64.shift();
      const mode = externalAttributes >>> 16;
      if ((mode & 0o170000) === 0o120000 || name.endsWith("/")) fail("archive_symlink", "Runtime ZIP contains a link or directory entry");
      if (names.has(name) || !expected.has(name)) fail("archive_inventory_mismatch", "Runtime ZIP contains an unexpected or duplicate path");
      names.add(name);
      entries.push({ name, method, compressedSize, uncompressedSize, localOffset });
      cursor = end;
    }
    if (cursor !== central.length || names.size !== expected.size) fail("archive_inventory_mismatch", "Runtime ZIP central directory differs");
    return entries;
  } finally {
    closeSync(descriptor);
  }
}

function parseZip64(extra, needsUncompressed, needsCompressed, needsOffset) {
  let cursor = 0;
  while (cursor + 4 <= extra.length) {
    const id = extra.readUInt16LE(cursor);
    const length = extra.readUInt16LE(cursor + 2);
    const value = extra.subarray(cursor + 4, cursor + 4 + length);
    if (cursor + 4 + length > extra.length) break;
    if (id === 0x0001) {
      const result = [];
      let position = 0;
      for (const needed of [needsUncompressed, needsCompressed, needsOffset]) {
        if (!needed) continue;
        if (position + 8 > value.length) fail("archive_invalid", "Runtime ZIP64 entry is incomplete");
        const integer = value.readBigUInt64LE(position);
        if (integer > BigInt(Number.MAX_SAFE_INTEGER)) fail("archive_too_large", "Runtime ZIP entry is too large");
        result.push(Number(integer));
        position += 8;
      }
      return result;
    }
    cursor += 4 + length;
  }
  if (needsUncompressed || needsCompressed || needsOffset) fail("archive_invalid", "Runtime ZIP64 metadata is missing");
  return [];
}

async function extractZip(archivePath, destination, manifest) {
  const expected = new Map(manifest.payload.files.map((entry) => [entry.path, entry]));
  const entries = parseZipEntries(archivePath, expected);
  mkdirSync(destination, { recursive: false, mode: 0o700 });
  const archiveDescriptor = openSync(archivePath, "r");
  try {
    for (const entry of entries) {
      const header = Buffer.alloc(30);
      readExactly(archiveDescriptor, header, 0, header.length, entry.localOffset);
      if (header.readUInt32LE(0) !== 0x04034b50 || header.readUInt16LE(8) !== entry.method) fail("archive_invalid", "Runtime ZIP local header differs");
      const nameLength = header.readUInt16LE(26);
      const extraLength = header.readUInt16LE(28);
      const localName = Buffer.alloc(nameLength);
      readExactly(archiveDescriptor, localName, 0, nameLength, entry.localOffset + 30);
      if (localName.toString("utf8") !== entry.name) fail("archive_invalid", "Runtime ZIP path identity differs");
      const dataStart = entry.localOffset + 30 + nameLength + extraLength;
      const destinationPath = safeDestination(destination, entry.name);
      mkdirSync(dirname(destinationPath), { recursive: true, mode: 0o700 });
      const expectedEntry = expected.get(entry.name);
      let written = 0;
      const digest = createHash("sha256");
      const meter = new Transform({
        transform(chunk, _encoding, callback) {
          written += chunk.length;
          if (written > expectedEntry.size) return callback(new BootstrapError("archive_file_size_mismatch", "Runtime ZIP member expands beyond its signed size"));
          digest.update(chunk);
          callback(null, chunk);
        },
      });
      const output = createWriteStream(destinationPath, { flags: "wx", mode: expectedEntry.executable ? 0o755 : 0o644 });
      if (entry.compressedSize === 0) {
        output.end();
        await new Promise((accept, reject) => { output.on("close", accept); output.on("error", reject); });
      } else {
        const input = createReadStream(archivePath, { start: dataStart, end: dataStart + entry.compressedSize - 1 });
        await (entry.method === 8 ? pipeline(input, createInflateRaw(), meter, output) : pipeline(input, meter, output));
      }
      if (written !== expectedEntry.size || written !== entry.uncompressedSize || `sha256:${digest.digest("hex")}` !== expectedEntry.sha256) {
        fail("archive_file_digest_mismatch", "Runtime ZIP member differs from its signed identity");
      }
      if (process.platform !== "win32") chmodSync(destinationPath, expectedEntry.executable ? 0o755 : 0o644);
    }
  } finally {
    closeSync(archiveDescriptor);
  }
}

function safeDestination(root, relative) {
  const destination = resolve(root, ...relative.split("/"));
  const prefix = resolve(root) + sep;
  if (!destination.startsWith(prefix)) fail("archive_path_escape", "Runtime archive path escapes the candidate root");
  return destination;
}

async function verifyTree(root, manifest) {
  for (const entry of manifest.payload.files) {
    const path = safeDestination(root, entry.path);
    let metadata;
    try {
      let current = resolve(root);
      for (const part of entry.path.split("/")) {
        current = join(current, part);
        const component = lstatSync(current);
        if (component.isSymbolicLink()) fail("runtime_symlink", "installed Runtime contains a symlink");
      }
      metadata = lstatSync(path);
    } catch (error) {
      if (error instanceof BootstrapError) throw error;
      fail("runtime_file_missing", "installed Runtime file is missing");
    }
    if (!metadata.isFile() || metadata.size !== entry.size || await fileDigest(path) !== entry.sha256) fail("runtime_file_digest_mismatch", "installed Runtime file differs");
  }
}

function atomicJson(path, value, mode = 0o600) {
  mkdirSync(dirname(path), { recursive: true, mode: 0o700 });
  const temporary = join(dirname(path), `.${basename(path)}.${randomUUID()}.tmp`);
  try {
    writeFileSync(temporary, `${canonical(value)}\n`, { flag: "wx", mode });
    renameSync(temporary, path);
    if (process.platform !== "win32") chmodSync(path, mode);
  } finally {
    rmSync(temporary, { force: true });
  }
}

function runtimeDirectory(runtimeId) {
  if (!isDigest(runtimeId)) fail("runtime_identity_invalid", "Runtime ID is invalid");
  return runtimeId.slice("sha256:".length);
}

function readPointer(path) {
  let value;
  try { value = JSON.parse(readFileSync(path, "utf8")); } catch { fail("runtime_pointer_invalid", "active Runtime pointer is invalid"); }
  exactKeys(value, ["schema", "current_runtime_id", "previous_runtime_id"], "runtime_pointer_invalid");
  if (value.schema !== "edgepilot-runtime-pointer-v1" || !isDigest(value.current_runtime_id) || (value.previous_runtime_id !== null && !isDigest(value.previous_runtime_id))) {
    fail("runtime_pointer_invalid", "active Runtime pointer identity is invalid");
  }
  return value;
}

async function withInstallLock(stateRoot, action) {
  return withStateLock(stateRoot, "install.lock", action);
}

export async function withLifecycleLock(stateRoot, action) {
  return withStateLock(stateRoot, "lifecycle.lock", action, 19 * 60_000);
}

async function withStateLock(stateRoot, name, action, timeoutMs = 180_000) {
  mkdirSync(stateRoot, { recursive: true, mode: 0o700 });
  const lock = join(stateRoot, name);
  const deadline = Date.now() + timeoutMs;
  while (true) {
    try {
      mkdirSync(lock, { mode: 0o700 });
      break;
    } catch (error) {
      if (error?.code !== "EEXIST") throw error;
      let owner;
      try { owner = JSON.parse(readFileSync(join(lock, "owner.json"), "utf8")); } catch { owner = null; }
      // A competing mkdir owner may not have written owner.json yet.
      if (owner === null) {
        try { if (Date.now() - lstatSync(lock).mtimeMs < 5_000) { await new Promise((accept) => setTimeout(accept, 50)); continue; } } catch { continue; }
      }
      const birth = owner !== null && Number.isSafeInteger(owner.pid) ? processBirth(owner.pid) : null;
      if (owner === null || !Number.isSafeInteger(owner.pid) || !processExists(owner.pid)
          || (typeof owner.birth === "string" && birth !== null && owner.birth !== birth)) {
        const stale = `${lock}.stale-${randomUUID()}`;
        try { renameSync(lock, stale); rmSync(stale, { recursive: true, force: true }); } catch { /* another waiter recovered it */ }
        continue;
      }
      if (Date.now() >= deadline) fail("runtime_busy", "another Runtime installation did not finish in time");
      await new Promise((accept) => setTimeout(accept, 100));
    }
  }
  const ownerId = randomUUID();
  atomicJson(join(lock, "owner.json"), { schema: "edgepilot-bootstrap-lock-v1", pid: process.pid, birth: processBirth(process.pid), owner_id: ownerId });
  try { return await action(); } finally {
    let current;
    try { current = JSON.parse(readFileSync(join(lock, "owner.json"), "utf8")); } catch { current = null; }
    if (current?.owner_id === ownerId) rmSync(lock, { recursive: true, force: true });
  }
}

function processBirth(pid) {
  if (!Number.isSafeInteger(pid) || pid <= 0) return null;
  if (process.platform === "linux") {
    try { const fields = readFileSync(`/proc/${pid}/stat`, "utf8").split(") ")[1].split(" "); return `linux:${fields[19]}`; } catch { return null; }
  }
  if (process.platform === "darwin") {
    const result = spawnSync("/bin/ps", ["-o", "lstart=", "-p", String(pid)], { encoding: "utf8", timeout: 1000 });
    return result.status === 0 && result.stdout.trim() ? `macos:${result.stdout.trim()}` : null;
  }
  if (process.platform === "win32") {
    const powershell = join(process.env.SYSTEMROOT ?? "C:\\Windows", "System32", "WindowsPowerShell", "v1.0", "powershell.exe");
    const result = spawnSync(powershell, ["-NoProfile", "-NonInteractive", "-Command", `(Get-Process -Id ${pid} -ErrorAction Stop).StartTime.ToUniversalTime().Ticks`], { encoding: "utf8", timeout: 3000, windowsHide: true });
    return result.status === 0 && /^\d+$/.test(result.stdout.trim()) ? `windows:${result.stdout.trim()}` : null;
  }
  return null;
}

function processExists(pid) {
  try { process.kill(pid, 0); return true; } catch (error) { return error?.code === "EPERM"; }
}

function directoryBytes(root) {
  let total = 0;
  for (const entry of readdirSync(root, { withFileTypes: true })) {
    const path = join(root, entry.name);
    const metadata = lstatSync(path);
    if (metadata.isSymbolicLink()) fail("runtime_path_invalid", "Runtime lifecycle root contains a symbolic link");
    if (metadata.isDirectory()) total += directoryBytes(path);
    else if (metadata.isFile()) total += metadata.size;
  }
  return total;
}

// -- v1 trading state (v2/11 section 5, v2/13 section 4; D46) ------------------------------
const LEGACY_MAINTENANCE = "/edgepilot/runtime_maintenance.py";
const LEGACY_JOBS = "runtime-live-process-jobs";

function readSmallJson(path) {
  try {
    const metadata = lstatSync(path);
    return metadata.isFile() && metadata.size <= 1024 * 1024 ? JSON.parse(readFileSync(path, "utf8")) : null;
  } catch { return null; }
}

/** Whether v1 trading state that has not been handed over still records unfinished tasks. */
export function legacyTradingStatePresent(liveStateRoot) {
  const root = join(liveStateRoot, LEGACY_JOBS);
  if (!existsSync(root)) return false;
  if (lstatSync(root).isSymbolicLink() || !lstatSync(root).isDirectory()) fail("legacy_state_invalid", "Legacy trading job root is invalid");
  return readdirSync(root).some((name) => /^job_[A-Za-z0-9_-]{20,128}\.json$/u.test(name)
    && !["succeeded", "completed", "failed", "cancelled"].includes(readSmallJson(join(root, name))?.state));
}

/** v1 trading tasks, asked from the old Runtime's own maintenance entry. */
async function legacyTradingJobs(old, liveStateRoot, operation = "inspect", options = {}) {
  if (old === null || !old.manifest.payload.files.some((file) => file.path.endsWith(LEGACY_MAINTENANCE)) || !existsSync(join(liveStateRoot, LEGACY_JOBS)))
    return { jobs: [], pinned_runtime_ids: [] };
  return runLiveMaintenance({ python: old.python, liveStateRoot, runtimeId: old.manifest.runtime_id, operation, env: cleanHostEnvironment(), ...options });
}

/** Move v1 trading state into ``tradingd/legacy/<time>/`` after its processes are gone. */
export function archiveLegacyTradingState(liveStateRoot, now = new Date()) {
  const stamp = now.toISOString().replaceAll(/[-:]/gu, "").replace(/\.\d+Z$/u, "Z");
  const archive = join(liveStateRoot, "tradingd", "legacy", stamp);
  const moved = [];
  const move = (relative) => {
    const source = join(liveStateRoot, relative);
    if (!existsSync(source)) return;
    if (lstatSync(source).isSymbolicLink()) fail("legacy_state_invalid", "Legacy trading state contains a link");
    const target = join(archive, relative);
    mkdirSync(dirname(target), { recursive: true, mode: 0o700 });
    renameSync(source, target);
    moved.push(relative.split(sep).join("/"));
  };
  move(LEGACY_JOBS);
  move("runtime-live-intents");
  const roots = ["strategies"];
  const accounts = join(liveStateRoot, "accounts");
  if (existsSync(accounts) && lstatSync(accounts).isDirectory())
    for (const entry of readdirSync(accounts, { withFileTypes: true })) if (entry.isDirectory()) roots.push(join("accounts", entry.name, "strategies"));
  for (const root of roots) {
    const absolute = join(liveStateRoot, root);
    if (!existsSync(absolute) || !lstatSync(absolute).isDirectory() || lstatSync(absolute).isSymbolicLink()) continue;
    move(join(root, ".locks", "trading-start.lock"));
    for (const strategy of readdirSync(absolute, { withFileTypes: true })) {
      if (!strategy.isDirectory() || strategy.name.startsWith(".")) continue;
      move(join(root, strategy.name, "running.json"));
      const runs = join(absolute, strategy.name, "runs");
      if (!existsSync(runs) || !lstatSync(runs).isDirectory()) continue;
      for (const run of readdirSync(runs, { withFileTypes: true })) {
        if (!run.isDirectory()) continue;
        const mode = readSmallJson(join(runs, run.name, "run.json"))?.mode ?? readSmallJson(join(runs, run.name, "execution.json"))?.mode;
        if (mode === "demo" || mode === "live") move(join(root, strategy.name, "runs", run.name));
      }
    }
  }
  return { archive: moved.length ? archive : null, moved };
}

function removeGeneratedFilesystemMetadata(root, errorCode) {
  for (const entry of readdirSync(root, { withFileTypes: true })) {
    if (!GENERATED_FILESYSTEM_METADATA.has(entry.name)) continue;
    const path = join(root, entry.name);
    const metadata = lstatSync(path);
    if (!metadata.isFile() || metadata.isSymbolicLink()) fail(errorCode, "Generated filesystem metadata is invalid");
    rmSync(path, { force: true });
  }
}

export async function garbageCollect({ stateRoot, pluginStateRoot = null, maximumReleases = 1, maximumBytes = 5 * 1024 ** 3 }) {
  if (!Number.isSafeInteger(maximumReleases) || maximumReleases < 1 || !Number.isSafeInteger(maximumBytes) || maximumBytes < 1) fail("gc_policy_invalid", "Runtime GC policy is invalid");
  return withInstallLock(stateRoot, async () => {
    const releases = join(stateRoot, "releases");
    if (!existsSync(releases)) return { removed: [], retained: [], bytes: 0 };
    await recoverRepairBackups(releases);
    removeGeneratedFilesystemMetadata(releases, "runtime_release_root_invalid");
    const pointer = readPointer(join(stateRoot, "current.json"));
    // A release whose interpreter still runs (a task finishing on the old Runtime, D44) is kept below.
    const protectedIds = new Set([pointer.current_runtime_id]);
    const releaseEntries = readdirSync(releases, { withFileTypes: true });
    for (const entry of releaseEntries.filter((item) => item.name.startsWith(".candidate-"))) {
      const path = join(releases, entry.name);
      if (!entry.isDirectory() || entry.isSymbolicLink() || lstatSync(path).isSymbolicLink()) fail("runtime_release_root_invalid", "Runtime candidate path is invalid");
      rmSync(path, { recursive: true, force: true });
    }
    const entries = releaseEntries.filter((entry) => !entry.name.startsWith(".candidate-")).map((entry) => {
      if (!/^[0-9a-f]{64}$/u.test(entry.name) || !entry.isDirectory() || entry.isSymbolicLink()) fail("runtime_release_root_invalid", "Runtime releases root contains an unmanaged entry");
      const path = join(releases, entry.name);
      const metadata = lstatSync(path);
      return { runtimeId: `sha256:${entry.name}`, path, bytes: directoryBytes(path), modified: metadata.mtimeMs };
    }).sort((left, right) => right.modified - left.modified);
    const keep = new Set(protectedIds);
    const executableById = new Map(entries.filter(entry => !keep.has(entry.runtimeId)).map(entry => {
      const manifest = validateManifest(JSON.parse(readFileSync(join(entry.path, "RUNTIME.json"), "utf8")), null, { enforcePlatform: false });
      if (manifest.runtime_id !== entry.runtimeId) fail("runtime_identity_invalid", "Cleanup Runtime identity differs");
      return [entry.runtimeId, safeDestination(entry.path, manifest.payload.python.executable)];
    }));
    const inUse = executableById.size ? runtimeExecutablesInUse([...executableById.values()]) : new Set();
    for (const [id, executable] of executableById) if (inUse.has(executable)) keep.add(id);
    for (const entry of entries) if (keep.size < maximumReleases) keep.add(entry.runtimeId);
    let retainedBytes = entries.filter((entry) => keep.has(entry.runtimeId)).reduce((sum, entry) => sum + entry.bytes, 0);
    const removed = [];
    for (const entry of [...entries].reverse()) {
      if (keep.has(entry.runtimeId)) continue;
      if (entries.length - removed.length <= maximumReleases && retainedBytes <= maximumBytes) continue;
      rmSync(entry.path, { recursive: true, force: true });
      removed.push(entry.runtimeId);
    }
    for (const root of [join(dirname(stateRoot), "downloads"), join(stateRoot, "probes")]) {
      if (!existsSync(root)) continue;
      if (lstatSync(root).isSymbolicLink() || !lstatSync(root).isDirectory()) fail("runtime_path_invalid", "Runtime temporary root is invalid");
      for (const name of readdirSync(root)) {
        const path = join(root, name);
        if (lstatSync(path).isSymbolicLink()) fail("runtime_path_invalid", "Runtime temporary path is a symbolic link");
        rmSync(path, { recursive: true, force: true });
      }
    }
    const pluginStagesRemoved = pluginStateRoot === null ? [] : garbageCollectPluginStages(pluginStateRoot);
    return { removed: removed.sort(), retained: entries.filter((entry) => !removed.includes(entry.runtimeId)).map((entry) => entry.runtimeId).sort(), bytes: retainedBytes, plugin_stages_removed: pluginStagesRemoved };
  });
}

function garbageCollectPluginStages(pluginStateRoot) {
  const stages = join(pluginStateRoot, "stages");
  if (!existsSync(stages)) return [];
  if (lstatSync(stages).isSymbolicLink() || !lstatSync(stages).isDirectory()) fail("plugin_state_invalid", "plugin stages root is invalid");
  removeGeneratedFilesystemMetadata(stages, "plugin_state_invalid");
  const keep = new Set();
  for (const profile of ["research", "live"]) {
    try {
      const value = JSON.parse(readFileSync(join(pluginStateRoot, `current-${profile}.json`), "utf8"));
      for (const key of ["current_stage_id", "previous_stage_id"]) if (isDigest(value[key])) keep.add(value[key].slice(7));
    } catch { /* a missing profile pointer protects nothing */ }
  }
  const removed = [];
  for (const entry of readdirSync(stages, { withFileTypes: true })) {
    const path = join(stages, entry.name);
    if (entry.name.startsWith(".candidate-")) {
      if (!entry.isDirectory() || entry.isSymbolicLink()) fail("plugin_state_invalid", "plugin candidate is invalid");
      rmSync(path, { recursive: true, force: true }); removed.push(entry.name); continue;
    }
    if (!/^[0-9a-f]{64}$/u.test(entry.name) || !entry.isDirectory() || entry.isSymbolicLink()) fail("plugin_state_invalid", "plugin stages contain an unmanaged entry");
    if (!keep.has(entry.name)) { rmSync(path, { recursive: true, force: true }); removed.push(entry.name); }
  }
  return removed.sort();
}

function rotateHostLog(logPath, maximumBytes = 10 * 1024 * 1024, retainedFiles = 3) {
  if (!existsSync(logPath) || statSync(logPath).size <= maximumBytes) return;
  for (let index = retainedFiles - 1; index >= 1; index -= 1) {
    const source = `${logPath}.${index}`;
    if (existsSync(source)) renameSync(source, `${logPath}.${index + 1}`);
  }
  renameSync(logPath, `${logPath}.1`);
}

async function recoverRepairBackups(releases, trustedKeys = null, enforcePlatform = true) {
  for (const entry of readdirSync(releases, { withFileTypes: true })) {
    if (!/^\.repair-[0-9a-f]{64}$/u.test(entry.name)) continue;
    if (!entry.isDirectory() || entry.isSymbolicLink()) fail("runtime_path_invalid", "Runtime repair backup is invalid");
    const backup = join(releases, entry.name), final = join(releases, entry.name.slice(8));
    if (!existsSync(final)) {
      renameSync(backup, final);
    } else {
      const manifest = validateManifest(JSON.parse(readFileSync(join(final, "RUNTIME.json"), "utf8")), trustedKeys, { enforcePlatform });
      if (manifest.runtime_id !== `sha256:${entry.name.slice(8)}`) fail("runtime_identity_invalid", "Runtime repair recovery identity differs");
      await verifyTree(final, manifest);
      rmSync(backup, { recursive: true, force: true });
    }
  }
}

export async function installRuntime({ archivePath, manifestPath, stateRoot, trustedKeys, enforcePlatform = true, probe = probeRuntime, beforeActivate = null, repair = false, activate = true }) {
  const manifestBytes = readFileSync(manifestPath);
  if (manifestBytes.length < 2 || manifestBytes.length > MAX_MANIFEST_BYTES) fail("manifest_size_invalid", "Runtime manifest size is invalid");
  let parsed;
  try { parsed = JSON.parse(manifestBytes.toString("utf8")); } catch { fail("manifest_json_invalid", "Runtime manifest is not strict JSON"); }
  const manifest = validateManifest(parsed, trustedKeys, { enforcePlatform });
  const archiveMetadata = statSync(archivePath);
  if (!archiveMetadata.isFile() || archiveMetadata.size !== manifest.payload.archive_size || await fileDigest(archivePath) !== manifest.payload.archive_sha256) {
    fail("archive_digest_mismatch", "Runtime archive differs from the signed manifest");
  }
  return withInstallLock(stateRoot, async () => {
    const releases = join(stateRoot, "releases");
    mkdirSync(releases, { recursive: true, mode: 0o700 });
    await recoverRepairBackups(releases, trustedKeys, enforcePlatform);
    const final = join(releases, runtimeDirectory(manifest.runtime_id));
    validateWindowsReleasePathBudget(final, manifest);
    const pointerPath = join(stateRoot, "current.json");
    let previous = null;
    if (existsSync(pointerPath)) {
      previous = readPointer(pointerPath);
    }
    if (previous !== null && previous.current_runtime_id !== manifest.runtime_id) {
      const currentPath = join(releases, runtimeDirectory(previous.current_runtime_id), "RUNTIME.json");
      let current;
      try {
        current = validateManifest(JSON.parse(readFileSync(currentPath, "utf8")), trustedKeys, { enforcePlatform });
      } catch (error) {
        if (error instanceof BootstrapError) throw error;
        fail("runtime_identity_invalid", "installed Runtime manifest is invalid");
      }
      if (current.runtime_id !== previous.current_runtime_id) fail("runtime_identity_invalid", "installed Runtime identity differs from its pointer");
      if (compareSemver(manifest.payload.release_version, current.payload.release_version) < 0) {
        fail("runtime_downgrade_forbidden", "Runtime installation cannot implicitly downgrade the active release");
      }
    }
    let reused = existsSync(final) && !repair;
    let activated = false;
    if (reused) {
      await verifyTree(final, manifest);
      const installed = JSON.parse(readFileSync(join(final, "RUNTIME.json"), "utf8"));
      if (canonical(installed) !== canonical(manifest)) fail("runtime_identity_invalid", "installed Runtime manifest differs");
      await probe(final, manifest, stateRoot);
    } else {
      // Keep the temporary Windows import path below DLL loader limits; the
      // activated content-addressed release path remains unchanged.
      const candidate = join(releases, `.c-${randomUUID().slice(0, 12)}`);
      try {
        await extractZip(archivePath, candidate, manifest);
        await verifyTree(candidate, manifest);
        atomicJson(join(candidate, "RUNTIME.json"), manifest);
        await probe(candidate, manifest, stateRoot);
        if (beforeActivate !== null) await beforeActivate(manifest);
        const backup = join(releases, `.repair-${runtimeDirectory(manifest.runtime_id)}`);
        const replacing = repair && existsSync(final);
        if (replacing) renameSync(final, backup);
        try { renameSync(candidate, final); } catch (error) {
          if (replacing) renameSync(backup, final);
          throw error;
        }
        activated = true;
        if (replacing) rmSync(backup, { recursive: true, force: true });
      } catch (error) {
        if (error?.code === "ENOSPC") fail("disk_full", "Runtime installation stopped because disk space is exhausted");
        throw error;
      } finally {
        rmSync(candidate, { recursive: true, force: true });
      }
    }
    if (!activated && beforeActivate !== null) await beforeActivate(manifest);
    if (activate) atomicJson(pointerPath, {
      schema: "edgepilot-runtime-pointer-v1",
      current_runtime_id: manifest.runtime_id,
      previous_runtime_id: previous?.current_runtime_id === manifest.runtime_id ? previous?.previous_runtime_id ?? null : previous?.current_runtime_id ?? null,
    });
    return { runtimeRoot: final, manifest, reused, previousPointer: previous };
  });
}

export async function prepareBoundRuntime({ home, stateRoot, channelUrl, product, version, runtimeIds, repair = false }) {
  if (!isSemver(version) || runtimeIds.length === 0 || runtimeIds.some(id => !isDigest(id))) fail("runtime_binding_missing", "A fixed release binding is required");
  const os = process.platform === "darwin" ? "macos" : process.platform === "win32" ? "windows" : "linux";
  const arch = process.arch === "x64" ? "amd64" : process.arch;
  const base = new URL(`../releases/${version}/${os}-${arch}/`, channelUrl);
  const downloads = join(home, "downloads");
  mkdirSync(downloads, { recursive: true, mode: 0o700 });
  const manifestPath = join(downloads, `manifest-${randomUUID()}.json`);
  const archivePath = join(downloads, `runtime-${randomUUID()}.zip`);
  {
    for (const id of runtimeIds) {
      for (const cacheRoot of [stateRoot, join(stateRoot, "prepared")]) {
        if (repair && cacheRoot === stateRoot) continue;
        try {
          const cached = await runtimeById(cacheRoot, id, null);
          if (manifestProduct(cached.manifest) === product && cached.manifest.payload.release_version === version) {
            try {
              await verifyTree(cached.root, cached.manifest);
              return { runtimeRoot: cached.root, manifest: cached.manifest, reused: true };
            } catch (error) {
              if (error?.code !== "runtime_file_missing" && error?.code !== "runtime_file_digest_mismatch") throw error;
            }
          }
        } catch (error) {
          if (error?.code === "EACCES" || error?.code === "EPERM") throw error;
        }
      }
    }
  }
  try {
    await download(new URL("RUNTIME.json", base).href, manifestPath, MAX_MANIFEST_BYTES);
    const manifest = validateManifest(JSON.parse(readFileSync(manifestPath, "utf8")), null);
    if (manifestProduct(manifest) !== product || manifest.payload.release_version !== version || !runtimeIds.includes(manifest.runtime_id)) fail("runtime_identity_incompatible", "Fixed release identity differs");
    await download(new URL("runtime.zip", base).href, archivePath, manifest.payload.archive_size, RUNTIME_DOWNLOAD_TIMEOUT_MS);
    return await installRuntime({ archivePath, manifestPath, stateRoot: join(stateRoot, "prepared"), trustedKeys: null, repair, activate: false });
  } finally {
    rmSync(manifestPath, { force: true });
    rmSync(archivePath, { force: true });
  }
}

async function switchHostIdentity(pluginStateRoot, product) {
  const connection = registeredConnection(pluginStateRoot, product);
  if (!connection || !await probeConnection(join(pluginStateRoot, "connections", `${product}.json`), connection.runtime_id)) return null;
  const authority = JSON.parse(readFileSync(join(pluginStateRoot, "connection-authority.json"), "utf8"));
  const token = authority[`${product}_app`];
  if (typeof token !== "string" || token.length < 40) fail("connection_state_invalid", "Host identity cannot be verified");
  const endpoint = new URL(connection.endpoint); endpoint.pathname = "/host/status";
  const response = await fetch(endpoint, { method: "POST", redirect: "error", signal: AbortSignal.timeout(2000),
    headers: { Authorization: ["Bearer", token].join(" "), "Content-Type": "application/json" }, body: "{}" });
  const identity = response.ok ? await response.json() : null;
  if (identity?.runtime_id !== connection.runtime_id || !Number.isSafeInteger(identity.pid) || identity.pid <= 0)
    fail("runtime_process_identity_unverified", "Host identity cannot be verified");
  return identity;
}

function switchResult(transaction, state) {
  return { schema: "edgepilot-bootstrap-result-v1", state, runtime_id: transaction.value.target_runtime_id,
    connection_ready: false, required_action: state === "awaiting_confirmation" ? "choose_runtime_switch" : null,
    switch: transaction.value.selection, lifecycle: transaction.value,
    choices: ["defer", "stop_and_continue"] };
}

/** The upgrade's readiness gate: release the trading hold once every engine migrated (D45). */
async function resumeTradingAfterUpgrade(home, product) {
  if (product !== "live") return null;
  const result = await tradingdCommand(home, ["--resume-after-upgrade", "--wait", "300"], 330_000);
  if (result?.result === "failed") fail("upgrade_trading_not_ready", `Trading stays paused: ${result.error?.code ?? "engines not ready"}`);
  return result?.result ?? null;
}

/**
 * After the commit point: start the trading service and the Host from the new Runtime,
 * pass the readiness gate and finish (v2/11 section 3 steps 6-8). Failures stay on the
 * new version; the next start repeats these steps.
 */
async function completeSwitch({ transaction, runtime, home, stateRoot, pluginStateRoot, liveStateRoot, researchStateRoot, product, marketplaceOrigin, environmentName, hostPort }) {
  transaction.advance("start", { cutover_started: true });
  const started = await startHost({ home, stateRoot, pluginStateRoot, liveStateRoot, researchStateRoot, trustedKeys: null, trustedKeyArguments: [],
    marketplaceOrigin, environmentName, hostPort, selectedRuntime: runtime });
  const trading = await resumeTradingAfterUpgrade(home, product);
  transaction.advance("ready", { blockers: [], last_error: null });
  try {
    await garbageCollect({ stateRoot, pluginStateRoot, maximumReleases: 1 });
    if (transaction.value.retired_directory) rmSync(join(stateRoot, "retired", transaction.value.retired_directory), { recursive: true, force: true });
  } catch { transaction.advance("ready", { cleanup_pending: true }); }
  return { schema: "edgepilot-bootstrap-result-v1", runtime_id: runtime.manifest.runtime_id, reused: runtime.reused ?? false, offline: runtime.reused ?? false,
    host: started, trading, lifecycle: transaction.value };
}

function previousRuntime(stateRoot, transaction, previousId, product) {
  if (!previousId) return null;
  const root = join(stateRoot, "releases", runtimeDirectory(previousId));
  const manifestPath = existsSync(join(root, "RUNTIME.json")) ? join(root, "RUNTIME.json")
    : transaction.value.retired_directory ? join(stateRoot, "retired", transaction.value.retired_directory, "RUNTIME.json") : null;
  if (!manifestPath) fail("runtime_identity_invalid", "Previous Runtime manifest is missing");
  const manifest = validateManifest(JSON.parse(readFileSync(manifestPath, "utf8")), null);
  if (manifest.runtime_id !== previousId || manifestProduct(manifest) !== product) fail("runtime_identity_invalid", "Previous Runtime identity differs");
  return { root, manifest, python: safeDestination(root, manifest.payload.python.executable) };
}

/**
 * Old Host processes by their command line (v1 Hosts also ran a worker and a Dashboard),
 * plus the Host's own process id as it reported it over the authenticated control channel.
 */
function oldHostProcesses(old, identity) {
  const found = old === null ? [] : runtimeProcessesByRole({ python: old.python, roles: ["host", "worker", "dashboard"], birthOf: processBirth });
  const birth = identity ? processBirth(identity.pid) : null;
  if (birth && !found.some((item) => item.pid === identity.pid)) found.push({ pid: identity.pid, birth, role: "host" });
  return found;
}

async function forwardUpgrade({ home, stateRoot, pluginStateRoot, liveStateRoot, researchStateRoot, product, environmentName, marketplaceOrigin, channelUrl, version, runtimeIds, repair, hostPort, choice = null }) {
  const previous = readLifecycleState(stateRoot);
  if (choice && (previous?.operation_id !== choice.operation_id || previous?.selection?.snapshot_digest !== choice.snapshot_digest
      || previous.target_version !== version || previous.selection.product !== product || previous.selection.environment !== environmentName
      || JSON.stringify(previous.target_ids) !== JSON.stringify(runtimeIds)))
    fail("runtime_switch_selection_stale", "The Runtime switch selection has changed");
  if (choice?.action === "defer") {
    if (previous.cutover_started) fail("runtime_switch_selection_stale", "A started cutover cannot be deferred");
    writeLifecycleState(join(stateRoot, "lifecycle.json"), { ...previous, phase: "deferred", updated_at: new Date().toISOString() });
    return switchResult({ value: { ...previous, phase: "deferred" } }, "deferred");
  }
  const transaction = new LifecycleTransaction(stateRoot, version, runtimeIds);
  let quiesced = false, oldRuntimeId = null;
  const switchArguments = { home, stateRoot, pluginStateRoot, liveStateRoot, researchStateRoot, product, marketplaceOrigin, environmentName, hostPort };
  try {
    const prepared = await prepareBoundRuntime({ home, stateRoot, channelUrl, product, version, runtimeIds, repair });
    const productState = product === "live" ? liveStateRoot : researchStateRoot;
    const stateMarker = join(productState, "runtime-state-format.json");
    if (existsSync(stateMarker)) {
      const metadata = lstatSync(stateMarker);
      if (!metadata.isFile() || metadata.isSymbolicLink() || metadata.size > 4096) fail("product_state_invalid", "Product state format is invalid");
      if (canonical(JSON.parse(readFileSync(stateMarker, "utf8"))) !== canonical({ schema: "edgepilot-product-state-format-v1", product, version: 1 }))
        fail("product_state_incompatible", "Product state is newer or belongs to another product");
    }
    transaction.advance("inspect", { target_runtime_id: prepared.manifest.runtime_id });
    const final = join(stateRoot, "releases", runtimeDirectory(prepared.manifest.runtime_id));
    let pointer = null;
    try { pointer = readPointer(join(stateRoot, "current.json")); } catch { /* first installation */ }
    const identity = await switchHostIdentity(pluginStateRoot, product);
    // Past the commit point already (or the new Host already runs from an interrupted
    // switch): continue from the start step, forward only.
    if (previous?.cutover_started && previous.phase !== "ready" && prepared.runtimeRoot === final
        && (pointer?.current_runtime_id === prepared.manifest.runtime_id || identity?.runtime_id === prepared.manifest.runtime_id)) {
      transaction.advance("commit", { cutover_started: true });
      atomicJson(join(stateRoot, "current.json"), { schema: "edgepilot-runtime-pointer-v1", current_runtime_id: prepared.manifest.runtime_id, previous_runtime_id: null });
      return await completeSwitch({ transaction, runtime: { root: final, manifest: prepared.manifest, reused: prepared.reused }, ...switchArguments });
    }
    const previousId = identity?.runtime_id ?? pointer?.current_runtime_id ?? null;
    oldRuntimeId = previousId;
    const old = previousRuntime(stateRoot, transaction, previousId, product);
    // A same-ID repair replaces byte-identical files: running engines are unaffected and
    // the trading service keeps running; only the Host restarts.
    const tradingAffected = product === "live" && previousId !== prepared.manifest.runtime_id;
    const inspect = async () => {
      const trading = tradingAffected ? await tradingdCommand(home, ["--status"]) : null;
      const legacy = product === "live" ? await legacyTradingJobs(old, liveStateRoot) : { jobs: [] };
      const running = legacy.jobs.filter((job) => job.runtime_in_use);
      const unverified = running.filter((job) => job.runtime_id !== previousId || job.process_evidence !== "running");
      if (legacy.truncated || unverified.length) {
        transaction.advance("inspect", { blockers: unverified.map((job) => ({ job_ref: job.job_ref, runtime_id: job.runtime_id, process_evidence: job.process_evidence })) });
        fail("runtime_process_identity_unverified", "An old task process cannot be verified");
      }
      const jobs = running.map((job) => ({ job_ref: job.job_ref, account_ref: job.account_ref, runtime_id: job.runtime_id, kind: job.kind }));
      // Open trading work pauses on upgrade: the user confirms it (D47).
      if (trading?.work_open) jobs.push({ job_ref: "trading_service", runtime_id: previousId, kind: "trading" });
      return { processes: [], jobs, trading, legacy: running };
    };
    let snapshot = await inspect();
    if (snapshot.jobs.length > 200) fail("runtime_process_inventory_exceeded", "Too many tasks to confirm in one switch");
    const authorized = transaction.value.authorized ?? (choice?.action === "stop_and_continue" ? previous.selection : null);
    if (snapshot.jobs.length && !selectionCovers(authorized, snapshot)) {
      switchSelection(transaction, { product, environment: environmentName, runtimeId: previousId, processes: [], jobs: snapshot.jobs });
      return switchResult(transaction, "awaiting_confirmation");
    }
    const stuck = (snapshot.trading?.engines ?? []).find((engine) => ["crash_loop", "unresponsive"].includes(engine.process));
    if (stuck) fail("trading_engine_unresponsive", "Restart the trading service before upgrading");
    if (authorized) transaction.advance("quiesce", { authorized });
    if (identity) {
      const admission = await controlHost(pluginStateRoot, previousId, product, "quiesce");
      quiesced = admission !== null;
      // Tasks keep running on the old Runtime (D44); only requests in flight block.
      if (admission?.blockers.some((item) => item.code !== "job_active")) fail("host_quiesce_failed", "Requests or invalid task state prevent switching");
    }
    transaction.advance("retire", { cutover_started: true, authorized });
    if (snapshot.legacy.length) {
      for (const job of snapshot.legacy)
        await legacyTradingJobs(old, liveStateRoot, "stop", { jobRef: job.job_ref, accountRef: job.account_ref,
          idempotencyKey: `runtime-switch-${transaction.value.operation_id}-${job.job_ref}` });
      snapshot = await inspect();
      if (snapshot.legacy.length) fail("runtime_process_in_use", "An old trading task survived the confirmed switch");
    }
    if (identity) await stopHost(pluginStateRoot, previousId, product);
    if (oldHostProcesses(old, identity).length) {
      // A Host that did not stop on request: the manager first, so it does not restart it
      // from the old launcher, then by its command line.
      serviceManager(home, product, environmentName).stop("host");
      const remaining = oldHostProcesses(old, identity);
      if (remaining.length) await stopAuthorizedProcesses(remaining, { birthOf: processBirth });
    }
    // The old Host (and a v1 Dashboard) read this state; move it only once they are gone.
    if (product === "live" && old !== null && existsSync(join(liveStateRoot, LEGACY_JOBS))) archiveLegacyTradingState(liveStateRoot);
    if (snapshot.trading?.running) {
      const drained = await tradingdCommand(home, ["--drain", "--hold-upgrade"], 180_000);
      if (drained?.result === "failed") fail("trading_service_drain_timeout", "The trading service did not drain");
    }
    if (prepared.runtimeRoot !== final) {
      mkdirSync(dirname(final), { recursive: true, mode: 0o700 });
      if (existsSync(final)) {
        const retired = join(stateRoot, "retired");
        mkdirSync(retired, { recursive: true, mode: 0o700 });
        const retiredName = `${runtimeDirectory(prepared.manifest.runtime_id)}-${randomUUID()}`;
        transaction.advance("retire", { retired_directory: retiredName });
        renameSync(final, join(retired, retiredName));
      }
      renameSync(prepared.runtimeRoot, final);
    }
    // Commit point: from here the switch only moves forward.
    transaction.advance("commit", { cutover_started: true });
    atomicJson(join(stateRoot, "current.json"), { schema: "edgepilot-runtime-pointer-v1", current_runtime_id: prepared.manifest.runtime_id, previous_runtime_id: null });
    return await completeSwitch({ transaction, runtime: { root: final, manifest: prepared.manifest, reused: prepared.reused }, ...switchArguments });
  } catch (error) {
    transaction.failure(error);
    if (quiesced && oldRuntimeId && !transaction.value.cutover_started) await controlHost(pluginStateRoot, oldRuntimeId, product, "resume");
    throw error;
  }
}

export async function controlHost(pluginStateRoot, runtimeId, product, action) {
  if (!["quiesce", "resume"].includes(action)) fail("maintenance_operation_invalid", "Invalid Host maintenance action");
  const connectionPath = join(pluginStateRoot, "connections", `${product}.json`);
  if (!await probeConnection(connectionPath, runtimeId)) return null;
  const connection = JSON.parse(readFileSync(connectionPath, "utf8"));
  const authority = JSON.parse(readFileSync(join(pluginStateRoot, "connection-authority.json"), "utf8"));
  const endpoint = new URL(connection.endpoint);
  endpoint.pathname = `/host/${action}`;
  const response = await fetch(endpoint, { method: "POST", redirect: "error", signal: AbortSignal.timeout(15000),
    headers: { Authorization: ["Bearer", authority[`${product}_app`]].join(" "), "Content-Type": "application/json" }, body: JSON.stringify({ runtime_id: runtimeId }) });
  if (response.status === 404) return null; // Supported first migration from a pre-control Host.
  if (!response.ok) fail("host_quiesce_failed", "Host maintenance admission failed");
  const value = await response.json();
  if (typeof value?.maintenance !== "boolean" || !Array.isArray(value.blockers)) fail("host_quiesce_failed", "Invalid Host admission response");
  return value;
}

export function runtimeInventoryPreflightScript() {
  // This runs before importing any Runtime code. Keep its policy aligned with
  // the Host verifier; declared legacy metadata is still integrity protected.
  return [
    "import json,pathlib,sys",
    "root=pathlib.Path(sys.argv[1])",
    "raw=json.loads((root/'RUNTIME.json').read_text(encoding='utf-8'))",
    "expected={item['path'] for item in raw['payload']['files']}",
    `generated_metadata=${JSON.stringify([...GENERATED_FILESYSTEM_METADATA])}`,
    "def runtime_inventory():",
    "    actual=set()",
    "    for path in root.rglob('*'):",
    "        relative=path.relative_to(root).as_posix()",
    "        if path.is_symlink() or path.is_junction(): raise RuntimeError('Runtime inventory contains a link')",
    "        if path.name in generated_metadata:",
    "            if not path.is_file(): raise RuntimeError('Runtime metadata is not a regular file')",
    "            if relative not in expected: continue",
    "        if path.is_file() and relative!='RUNTIME.json': actual.add(relative)",
    "        elif not path.is_file() and not path.is_dir(): raise RuntimeError('Runtime inventory contains a special file')",
    "    return actual",
    "actual=runtime_inventory()",
    "(_ for _ in ()).throw(RuntimeError(f'Runtime inventory preflight missing={sorted(expected-actual)[:3]} extra={sorted(actual-expected)[:3]}')) if expected!=actual else None",
  ].join("\n");
}

async function probeRuntime(runtimeRoot, manifest, stateRoot) {
  const python = safeDestination(runtimeRoot, manifest.payload.python.executable);
  const probeRoot = join(stateRoot, "probes", randomUUID());
  mkdirSync(probeRoot, { recursive: true, mode: 0o700 });
  const script = [
    runtimeInventoryPreflightScript(),
    "from edgepilot_runtime_host.contracts.runtime_manifest import RuntimeManifestEnvelope",
    "from edgepilot_runtime_host.release import RuntimeArtifactVerifier,RuntimePlatform",
    "from edgepilot_runtime_host.release.probe import RuntimeProbe",
    "after_import=runtime_inventory()",
    "(_ for _ in ()).throw(RuntimeError(f'Runtime import mutated tree dont_write={sys.dont_write_bytecode} extra={sorted(after_import-expected)[:10]}')) if after_import!=expected else None",
    "manifest=RuntimeManifestEnvelope.from_dict(raw)",
    "print('runtime_probe_stage=verify_tree',flush=True)",
    "RuntimeArtifactVerifier(RuntimePlatform.current()).verify_tree(root,manifest,allow_installed_manifest=True)",
    "print('runtime_probe_stage=product_probe',flush=True)",
    "RuntimeProbe(pathlib.Path(sys.argv[2]))(root,manifest)",
  ].join("\n");
  try {
    await new Promise((accept, reject) => {
      let tail = "runtime_probe_stage=starting\n";
      let settled = false;
      const child = spawn(python, ["-I", "-B", "-c", script, runtimeRoot, probeRoot], {
        stdio: ["ignore", "pipe", "pipe"],
        windowsHide: true,
        env: cleanHostEnvironment(),
      });
      const collect = (chunk) => { tail = (tail + chunk.toString("utf8")).slice(-8192); };
      child.stdout.on("data", collect);
      child.stderr.on("data", collect);
      const deadline = setTimeout(() => {
        if (settled) return;
        settled = true;
        child.kill("SIGKILL");
        atomicJson(join(stateRoot, "probe-failure.json"), { schema: "edgepilot-runtime-probe-failure-v1", code: "runtime_probe_timeout", diagnostic: tail });
        reject(new BootstrapError("runtime_probe_timeout", "Runtime probe timed out"));
      }, RUNTIME_PROBE_TIMEOUT_MS);
      child.once("error", (error) => {
        if (settled) return;
        settled = true;
        clearTimeout(deadline);
        atomicJson(join(stateRoot, "probe-failure.json"), { schema: "edgepilot-runtime-probe-failure-v1", code: "runtime_probe_start_failed", diagnostic: String(error?.code ?? "spawn_failed") });
        reject(error);
      });
      child.once("exit", (code) => {
        if (settled) return;
        settled = true;
        clearTimeout(deadline);
        if (code === 0) accept();
        else {
          atomicJson(join(stateRoot, "probe-failure.json"), { schema: "edgepilot-runtime-probe-failure-v1", code: "runtime_probe_failed", exit_code: code, diagnostic: tail || null });
          reject(new BootstrapError("runtime_probe_failed", "Runtime probe failed"));
        }
      });
    });
    rmSync(join(stateRoot, "probe-failure.json"), { force: true });
  } finally {
    rmSync(probeRoot, { recursive: true, force: true });
  }
}

async function download(url, destination, maximumBytes, timeoutMs = METADATA_DOWNLOAD_TIMEOUT_MS) {
  const parsed = validateFunctionalUrl(url);
  let response;
  try {
    response = await fetch(parsed, { redirect: "error", signal: AbortSignal.timeout(timeoutMs) });
  } catch (error) {
    fail(error?.name === "TimeoutError" ? "download_timeout" : "channel_unavailable", "Runtime source is unavailable");
  }
  if (!response.ok || !response.body) fail("download_failed", `Runtime download failed with HTTP ${response.status}`);
  const declared = Number(response.headers.get("content-length"));
  if (Number.isSafeInteger(declared) && declared > maximumBytes) fail("download_size_invalid", "Runtime download exceeds its signed size");
  let written = 0;
  const meter = new Transform({ transform(chunk, _encoding, callback) { written += chunk.length; callback(written > maximumBytes ? new BootstrapError("download_size_invalid", "Runtime download exceeds its signed size") : null, chunk); } });
  try {
    await pipeline(Readable.fromWeb(response.body), meter, createWriteStream(destination, { flags: "wx", mode: 0o600 }));
    return written;
  } catch (error) {
    rmSync(destination, { force: true });
    if (error instanceof BootstrapError) throw error;
    if (error?.code === "ENOSPC") fail("disk_full", "Runtime download stopped because disk space is exhausted");
    fail("download_interrupted", "Runtime download was interrupted");
  }
}

export async function installFromFunctionalChannel({ home, stateRoot, channelUrl, expectedProduct, expectedProductVersion = null, expectedRuntimeIds = [], beforeActivate = null, enforcePlatform = true, probe = probeRuntime, repair = false }) {
  const downloads = join(home, "downloads");
  mkdirSync(downloads, { recursive: true, mode: 0o700 });
  const channelPath = join(downloads, `channel-${randomUUID()}.json`);
  const manifestPath = join(downloads, `manifest-${randomUUID()}.json`);
  const archivePath = join(downloads, `runtime-${randomUUID()}.zip`);
  try {
    await download(channelUrl, channelPath, MAX_CHANNEL_BYTES);
    const selected = validateFunctionalChannel(JSON.parse(readFileSync(channelPath, "utf8")), channelUrl, { enforcePlatform, expectedProduct });
    let manifest = null;
    if (expectedProductVersion !== null && selected.target.release_version !== expectedProductVersion) {
      if (!isSemver(expectedProductVersion) || expectedRuntimeIds.length === 0) fail("runtime_version_incompatible", "channel release differs from plugin binding");
      // The mutable channel may advance before an already installed plugin is opened.
      // Its immutable release remains admissible only through the plugin's digest pin.
      const base = new URL(`../releases/${expectedProductVersion}/${selected.target.os}-${selected.target.arch}/`, channelUrl);
      const pinnedManifestUrl = new URL("RUNTIME.json", base).href;
      await download(pinnedManifestUrl, manifestPath, MAX_MANIFEST_BYTES);
      manifest = validateManifest(JSON.parse(readFileSync(manifestPath, "utf8")), null, { enforcePlatform });
      if (manifest.payload.release_version !== expectedProductVersion) fail("runtime_version_incompatible", "immutable release version differs");
      selected.target = { ...selected.target, runtime_id: manifest.runtime_id, release_version: expectedProductVersion, manifest_url: pinnedManifestUrl, archive_url: new URL("runtime.zip", base).href, archive_size: manifest.payload.archive_size, archive_sha256: manifest.payload.archive_sha256 };
    }
    if (expectedRuntimeIds.length > 0 && !expectedRuntimeIds.includes(selected.target.runtime_id)) fail("runtime_identity_incompatible", "channel Runtime differs from plugin release binding");
    try {
      const active = await activeRuntime(stateRoot, null);
      if (manifestProduct(active.manifest) !== expectedProduct) fail("runtime_product_incompatible", "installed Runtime belongs to another product");
      if (!repair && active.manifest.runtime_id === selected.target.runtime_id) {
        atomicJson(join(stateRoot, "channel.json"), { schema: "edgepilot-installed-channel-v1", channel: selected.channel.channel, runtime_id: active.manifest.runtime_id });
        return { runtimeRoot: active.root, manifest: active.manifest, reused: true, previousPointer: readPointer(join(stateRoot, "current.json")) };
      }
    } catch { /* a missing or invalid active Runtime is installed below */ }
    if (manifest === null) {
      await download(selected.target.manifest_url, manifestPath, MAX_MANIFEST_BYTES);
      manifest = validateManifest(JSON.parse(readFileSync(manifestPath, "utf8")), null, { enforcePlatform });
    }
    if (manifestProduct(manifest) !== expectedProduct) fail("runtime_product_incompatible", "Runtime manifest contains another product profile");
    if (manifest.runtime_id !== selected.target.runtime_id || manifest.payload.archive_size !== selected.target.archive_size || manifest.payload.archive_sha256 !== selected.target.archive_sha256) fail("channel_runtime_mismatch", "Runtime manifest differs from channel");
    await download(selected.target.archive_url, archivePath, selected.target.archive_size, RUNTIME_DOWNLOAD_TIMEOUT_MS);
    const installed = await installRuntime({ archivePath, manifestPath, stateRoot, trustedKeys: null, beforeActivate, enforcePlatform, probe, repair });
    atomicJson(join(stateRoot, "channel.json"), { schema: "edgepilot-installed-channel-v1", channel: selected.channel.channel, runtime_id: installed.manifest.runtime_id });
    return installed;
  } finally {
    for (const path of [channelPath, manifestPath, archivePath]) rmSync(path, { force: true });
  }
}

export async function installFromChannel({ home, stateRoot, config, runtimePin = null, pluginVersion = null, enforcePlatform = true, probe = probeRuntime }) {
  const downloads = join(home, "downloads");
  mkdirSync(downloads, { recursive: true, mode: 0o700 });
  const channelPath = join(downloads, `channel-${randomUUID()}.json`);
  const manifestPath = join(downloads, `manifest-${randomUUID()}.json`);
  const archivePath = join(downloads, `runtime-${randomUUID()}.zip`);
  try {
    await download(config.channel_url, channelPath, MAX_CHANNEL_BYTES);
    const channel = validateChannel(JSON.parse(readFileSync(channelPath, "utf8")), config.keys, config.channel_url, {
      requestedChannel: config.channel,
      runtimePin,
      pluginVersion,
      enforcePlatform,
    });
    await download(channel.target.manifest_url, manifestPath, MAX_MANIFEST_BYTES);
    const manifest = validateManifest(JSON.parse(readFileSync(manifestPath, "utf8")), config.keys, { enforcePlatform });
    if (manifest.runtime_id !== channel.target.runtime_id || manifest.payload.archive_size !== channel.target.archive_size || manifest.payload.archive_sha256 !== channel.target.archive_sha256) fail("channel_runtime_mismatch", "Runtime manifest differs from the signed channel selection");
    const selectedKey = config.keys.get(channel.target.signing_key_id);
    if (selectedKey === undefined) fail("channel_runtime_signer_mismatch", "Runtime signer differs from the signed channel selection");
    verifyEnvelopeSignatures(manifest.signatures, new Map([[channel.target.signing_key_id, selectedKey]]), Buffer.concat([MANIFEST_DOMAIN, canonicalBytes(manifest.payload)]), "Runtime manifest");
    await download(channel.target.archive_url, archivePath, channel.target.archive_size, RUNTIME_DOWNLOAD_TIMEOUT_MS);
    const installed = await installRuntime({ archivePath, manifestPath, stateRoot, trustedKeys: config.keys, enforcePlatform, probe });
    atomicJson(join(stateRoot, "channel.json"), {
      schema: "edgepilot-installed-channel-v1",
      channel: config.channel,
      channel_id: channel.envelope.channel_id,
      runtime_id: installed.manifest.runtime_id,
    });
    return installed;
  } finally {
    for (const path of [channelPath, manifestPath, archivePath]) rmSync(path, { force: true });
  }
}

async function activeRuntime(stateRoot, trustedKeys) {
  const pointer = readPointer(join(stateRoot, "current.json"));
  return runtimeById(stateRoot, pointer.current_runtime_id, trustedKeys);
}

async function runtimeById(stateRoot, runtimeId, trustedKeys) {
  const root = join(stateRoot, "releases", runtimeDirectory(runtimeId));
  const manifest = validateManifest(JSON.parse(readFileSync(join(root, "RUNTIME.json"), "utf8")), trustedKeys);
  if (manifest.runtime_id !== runtimeId) fail("runtime_identity_invalid", "Runtime directory and manifest differ");
  await verifyTree(root, manifest);
  return { root, manifest };
}

export async function probeConnection(path, expectedRuntimeId) {
  let connection;
  try { connection = JSON.parse(await readFile(path, "utf8")); } catch { return false; }
  if (connection.runtime_id !== expectedRuntimeId || !validControlConnection(connection)) return false;
  try {
    const response = await fetch(connection.endpoint, {
      method: "POST",
      headers: { Authorization: ["Bearer", connection.bearer_token].join(" "), "Content-Type": "application/json", "X-EdgePilot-Runtime-ID": expectedRuntimeId },
      body: JSON.stringify({ jsonrpc: "2.0", id: "bootstrap-probe", method: "initialize", params: { protocolVersion: "2025-06-18" } }),
      redirect: "error",
      signal: AbortSignal.timeout(1_000),
    });
    if (!response.ok) return false;
    const body = await response.json();
    return body?.result?.serverInfo?.runtimeId === expectedRuntimeId;
  } catch { return false; }
}

function validControlConnection(value, product = null) {
  if (!value || !isDigest(value.runtime_id) || typeof value.bearer_token !== "string") return false;
  try {
    const endpoint = new URL(value.endpoint);
    return endpoint.protocol === "http:" && endpoint.hostname === "127.0.0.1" && Boolean(endpoint.port)
      && !endpoint.username && !endpoint.password && !endpoint.search && !endpoint.hash
      && (product === null ? /^\/mcp\/(live|research)$/.test(endpoint.pathname) : endpoint.pathname === `/mcp/${product}`);
  } catch { return false; }
}


export async function stopHost(pluginStateRoot, runtimeId, product) {
  let connection;
  let authority;
  try {
    connection = JSON.parse(await readFile(join(pluginStateRoot, "connections", `${product}.json`), "utf8"));
    authority = JSON.parse(await readFile(join(pluginStateRoot, "connection-authority.json"), "utf8"));
  } catch {
    return false;
  }
  if (connection.runtime_id !== runtimeId) return false;
  if (!validControlConnection(connection, product)) return false;
  const appToken = authority[`${product}_app`];
  if (typeof appToken !== "string" || appToken.length < 40) return false;
  let endpoint;
  try {
    endpoint = new URL(connection.endpoint);
    endpoint.pathname = "/host/status";
    endpoint.search = "";
    endpoint.hash = "";
  } catch {
    return false;
  }
  try {
    const headers = { Authorization: ["Bearer", appToken].join(" "), "Content-Type": "application/json" };
    const status = await fetch(endpoint, { method: "POST", headers, body: "{}", redirect: "error", signal: AbortSignal.timeout(1_000) });
    const identity = status.ok ? await status.json() : null;
    if (identity?.runtime_id !== runtimeId) return false;
    endpoint.pathname = "/host/stop";
    const stopped = await fetch(endpoint, { method: "POST", headers, body: JSON.stringify({ runtime_id: runtimeId }), redirect: "error", signal: AbortSignal.timeout(2_000) });
    if (stopped.status !== 202) return false;
    const deadline = Date.now() + 5_000;
    while (Date.now() < deadline) {
      if (!await probeConnection(join(pluginStateRoot, "connections", `${product}.json`), runtimeId) && (!Number.isSafeInteger(identity.pid) || !processExists(identity.pid))) return true;
      await new Promise((accept) => setTimeout(accept, 50));
    }
  } catch {
    return false;
  }
  return false;
}

function hostArguments({ root, manifest, stateRoot, pluginStateRoot, liveStateRoot, researchStateRoot, trustedKeys, trustedKeyArguments, marketplaceOrigin, environmentName, hostPort, liveDashboardPort, researchDashboardPort }) {
  const product = manifestProduct(manifest);
  if (product === "live" && !marketplaceOrigin) fail("marketplace_origin_missing", "Live Runtime requires --marketplace-origin");
  const values = [
    "--runtime-state-root", stateRoot,
    "--runtime-id", manifest.runtime_id,
    "--plugin-state-root", pluginStateRoot,
    "--research-state-root", researchStateRoot,
    "--live-state-root", liveStateRoot,
    "--research-plugin-template", join(root, "plugins", "edgepilot-research"),
    "--live-plugin-template", join(root, "plugins", "edgepilot-live"),
    "--host-port", String(hostPort),
    "--environment", environmentName,
  ];
  if (marketplaceOrigin) values.push("--marketplace-origin", marketplaceOrigin);
  values.push("--live-dashboard-port", String(liveDashboardPort), "--research-dashboard-port", String(researchDashboardPort));
  if (trustedKeys === null) values.push("--functional-unsigned");
  for (const value of trustedKeyArguments) values.push("--trusted-key", value);
  return values;
}

function serviceManager(home, product, environmentName) {
  return new ServiceManager({ home, product, environment: environmentName });
}

/**
 * Rewrite the launcher for ``runtime`` and record how the Host may start and restart the
 * trading service (D40, D43). Runs on every start so moved Node or bootstrap paths heal.
 */
function refreshServices({ home, runtime, product, environmentName, marketplaceOrigin, liveStateRoot, researchStateRoot, hostArgs, mode }) {
  writeLauncher(home, {
    runtimeId: runtime.manifest.runtime_id,
    python: safeDestination(runtime.root, runtime.manifest.payload.python.executable),
    host: hostArgs,
    tradingd: product === "live" ? { stateRoot: liveStateRoot, environment: environmentName } : null,
    environment: capturedEnvironment(),
  });
  const common = ["--product", product, "--runtime-home", home, "--environment", environmentName,
    "--live-state-root", liveStateRoot, "--research-state-root", researchStateRoot,
    ...(marketplaceOrigin ? ["--marketplace-origin", marketplaceOrigin] : [])];
  const bootstrap = realpathSync(fileURLToPath(import.meta.url));
  const names = serviceManager(home, product, environmentName).names;
  const python = safeDestination(runtime.root, runtime.manifest.payload.python.executable);
  writeServicesRecord(home, {
    product, environment: environmentName, mode, launcher: launcherPath(home), services: names,
    // Windows runs trading service commands without the .vbs launcher (tradingdCommand).
    ...(product === "live" ? {
      tradingd_command: [python, "-I", "-B", "-m", "edgepilot.tradingd", "--environment", environmentName],
      tradingd_environment: { ...capturedEnvironment(), EDGEPILOT_HOME: liveStateRoot, EDGEPILOT_RUNTIME_ID: runtime.manifest.runtime_id },
    } : {}),
    control: product === "live" ? {
      start_tradingd: [process.execPath, bootstrap, "service", "start", "--service", "tradingd", ...common],
      restart_tradingd: [process.execPath, bootstrap, "service", "restart", "--service", "tradingd", ...common],
    } : {},
  });
}

/**
 * Run ``edgepilot.tradingd`` with a control flag through the launcher, so it resolves the
 * same socket directory as the service (D45). Returns the printed JSON, or null when the
 * launcher does not exist yet (nothing installed the trading service).
 */
export async function tradingdCommand(home, args, timeoutMs = 60_000) {
  const launcher = launcherPath(home);
  if (!existsSync(launcher)) return null;
  let command, commandArguments, environment = { ...cleanHostEnvironment() };
  if (process.platform === "win32") {
    // The .vbs launcher cannot return output; named pipes do not depend on the environment.
    const record = readServicesRecord(home);
    if (!record?.tradingd_command) return null;
    [command, ...commandArguments] = [...record.tradingd_command, ...args];
    environment = { ...environment, ...record.tradingd_environment };
  } else {
    [command, commandArguments] = ["/bin/sh", [launcher, "tradingd", ...args]];
  }
  return new Promise((accept, reject) => {
    const child = spawn(command, commandArguments, { stdio: ["ignore", "pipe", "ignore"], windowsHide: true, env: environment });
    let output = "";
    child.stdout.on("data", (chunk) => { output = (output + chunk.toString("utf8")).slice(-65536); });
    const timer = setTimeout(() => child.kill("SIGKILL"), timeoutMs);
    child.once("error", () => { clearTimeout(timer); reject(new BootstrapError("trading_service_command_failed", "Trading service command could not start")); });
    child.once("close", () => {
      clearTimeout(timer);
      try { accept(JSON.parse(output.trim().split("\n").at(-1))); }
      catch { reject(new BootstrapError("trading_service_command_failed", "Trading service command returned no result")); }
    });
  });
}

async function waitForTradingService(home, timeoutMs = 30_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const status = await tradingdCommand(home, ["--status"]);
    if (status?.running) return status;
    await new Promise((accept) => setTimeout(accept, 500));
  }
  fail("trading_service_start_timeout", "The trading service did not start");
}

export async function startHost({ home = null, stateRoot, pluginStateRoot, liveStateRoot, researchStateRoot, trustedKeys, trustedKeyArguments, marketplaceOrigin = null, environmentName = "production", hostPort = DEFAULT_HOST_PORT, selectedRuntime = null }) {
  const runtime = selectedRuntime ?? await activeRuntime(stateRoot, trustedKeys);
  const { manifest } = runtime;
  const product = manifestProduct(manifest);
  const runtimeHome = home ?? dirname(stateRoot);
  const liveDashboardPort = Number(process.env.EDGEPILOT_LIVE_DASHBOARD_PORT ?? 8787);
  const researchDashboardPort = Number(process.env.EDGEPILOT_RESEARCH_DASHBOARD_PORT ?? 8686);
  validateEnvironmentIsolation({
    product,
    environmentName,
    marketplaceOrigin,
    runtimeHome,
    liveStateRoot,
    researchStateRoot,
    liveDashboardPort,
    researchDashboardPort,
  });
  const hostArgs = hostArguments({ root: runtime.root, manifest, stateRoot, pluginStateRoot, liveStateRoot, researchStateRoot, trustedKeys,
    trustedKeyArguments, marketplaceOrigin, environmentName, hostPort, liveDashboardPort, researchDashboardPort });
  const connections = join(pluginStateRoot, "connections");
  const services = serviceManager(runtimeHome, product, environmentName);
  const refresh = (mode) => refreshServices({ home: runtimeHome, runtime, product, environmentName, marketplaceOrigin,
    liveStateRoot, researchStateRoot, hostArgs, mode });
  const running = await probeConnection(join(connections, `${product}.json`), manifest.runtime_id);
  if (!running && await switchHostIdentity(pluginStateRoot, product))
    fail("runtime_switch_confirmation_required", "A different Host must be confirmed before replacement");
  refresh(readServicesRecord(runtimeHome)?.mode ?? null);
  mkdirSync(join(stateRoot, "logs"), { recursive: true, mode: 0o700 });
  // The trading service keeps running across Host restarts; start it whenever Live is used.
  if (product === "live") {
    rotateHostLog(join(stateRoot, "logs", "tradingd.log"));
    services.ensureRunning("tradingd");
  }
  if (running) {
    if (services.mode !== null) refresh(services.mode);
    return { alreadyRunning: true, runtimeId: manifest.runtime_id };
  }
  rotateHostLog(join(stateRoot, "logs", "host.log"));
  services.ensureRunning("host");
  refresh(services.mode);
  const deadline = Date.now() + HOST_START_TIMEOUT_MS;
  while (Date.now() < deadline) {
    // The Host serves the product page itself; a ready Host is a ready page.
    if (await probeConnection(join(connections, `${product}.json`), manifest.runtime_id))
      return { alreadyRunning: false, runtimeId: manifest.runtime_id, service_mode: services.mode };
    await new Promise((accept) => setTimeout(accept, 100));
  }
  fail("host_start_timeout", "Runtime Host did not become ready");
}

function ensureLoopbackNoProxy(environment) {
  const result = { ...environment };
  const names = Object.keys(result).filter((key) => key.toUpperCase() === "NO_PROXY");
  if (names.length === 0) {
    result.NO_PROXY = "127.0.0.1";
    return result;
  }
  for (const name of names) {
    const raw = String(result[name] ?? "");
    const tokens = raw.split(",").map((item) => item.trim()).filter(Boolean);
    if (!tokens.some((item) => item.toLowerCase() === "127.0.0.1")) {
      result[name] = ["127.0.0.1", ...tokens].join(",");
    }
  }
  return result;
}

export function cleanHostEnvironment() {
  // Keep the account home available to Python's platform/path libraries while
  // continuing to drop inherited Python paths, credentials and unrelated
  // process state. Windows Python resolves Path.home() from USERPROFILE (or
  // HOMEDRIVE/HOMEPATH); Unix builds use HOME. USERPROFILE is not the user
  // environment: spawn({env}) replaces inheritance, so proxy variables must
  // be allowlisted or backtests cannot reach venue HTTP endpoints. Forwarded
  // HTTP(S)_PROXY would otherwise send Host/Dashboard loopback traffic through
  // the proxy, so NO_PROXY always contains 127.0.0.1.
  const allowed = new Set(["SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "LANG", "LC_ALL",
    "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "EDGEPILOT_PROXY_URL", "EDGEPILOT_PROXY_MODE"]);
  return {
    ...ensureLoopbackNoProxy(Object.fromEntries(Object.entries(process.env).filter(([key]) => allowed.has(key.toUpperCase())))),
    PYTHONDONTWRITEBYTECODE: "1",
    PYTHONNOUSERSITE: "1",
    PYTHONUTF8: "1",
  };
}

export async function doctor({ stateRoot, pluginStateRoot, liveStateRoot, trustedKeys, product }) {
  let active = null;
  let runtimeError = null;
  try { active = await activeRuntime(stateRoot, trustedKeys); } catch (error) { runtimeError = error instanceof BootstrapError ? error.code : "runtime_invalid"; }
  let channel = null;
  try { channel = JSON.parse(readFileSync(join(stateRoot, "channel.json"), "utf8")); } catch { channel = null; }
  const runtimeId = active?.manifest.runtime_id ?? null;
  const profiles = { [product]: runtimeId === null ? false : await probeConnection(join(pluginStateRoot, "connections", `${product}.json`), runtimeId) };
  let host = null;
  try {
    const owner = JSON.parse(readFileSync(join(stateRoot, "host.lock"), "utf8"));
    host = { pid: Number.isSafeInteger(owner.pid) ? owner.pid : null, running: Number.isSafeInteger(owner.pid) && processExists(owner.pid) };
  } catch { host = { pid: null, running: false }; }
  if (runtimeId !== null) {
    try {
      const connection = JSON.parse(readFileSync(join(pluginStateRoot, "connections", `${product}.json`), "utf8"));
      const authority = JSON.parse(readFileSync(join(pluginStateRoot, "connection-authority.json"), "utf8"));
      const endpoint = new URL(connection.endpoint); endpoint.pathname = "/host/status"; endpoint.search = ""; endpoint.hash = "";
      const response = await fetch(endpoint, { method: "POST", headers: { Authorization: ["Bearer", authority[`${product}_app`]].join(" "), "Content-Type": "application/json" }, body: "{}", redirect: "error", signal: AbortSignal.timeout(2_000) });
      if (response.ok) {
        const value = await response.json();
        if (value.runtime_id === runtimeId) host = { pid: value.pid, running: true, started_at: value.started_at, endpoint: `${endpoint.origin}`, products: value.products };
      }
    } catch { /* the process/connection summary above remains authoritative */ }
  }
  const logs = join(stateRoot, "logs");
  const logBytes = existsSync(logs) ? directoryBytes(logs) : 0;
  let recentError = null;
  try { recentError = JSON.parse(readFileSync(join(stateRoot, "probe-failure.json"), "utf8"))?.code ?? null; } catch { recentError = null; }
  return {
    schema: "edgepilot-runtime-doctor-v1",
    product,
    bootstrap_version: BOOTSTRAP_PRODUCT_VERSION,
    channel: channel === null ? null : { name: channel.channel, channel_id: typeof channel.channel_id === "string" ? channel.channel_id : null },
    runtime: active === null ? { runtime_id: null, error: runtimeError } : {
      runtime_id: runtimeId,
      release_version: active.manifest.payload.release_version,
      registry_digest: active.manifest.payload.operation_registry_digest,
      python: { implementation: active.manifest.payload.python.implementation, version: active.manifest.payload.python.version, abi: active.manifest.payload.python.abi },
      tree_verified: true,
    },
    host,
    connections: profiles,
    services: readServicesRecord(dirname(stateRoot))?.mode ?? null,
    legacy_trading_state: product === "live" ? legacyTradingStatePresent(liveStateRoot) : false,
    lifecycle: readLifecycleState(stateRoot),
    recent_error_code: recentError,
    logs: { directory: "runtime/logs", bytes: logBytes, maximum_file_bytes: 10 * 1024 * 1024, retained_files: 3 },
  };
}

/**
 * v2/11 section 4: refuse while trading work is unfinished, drain and unregister the
 * services, then delete the Runtime. Product state is kept; deleting it is left to the
 * user (D46).
 */
export async function uninstallRuntime({ stateRoot, pluginStateRoot, liveStateRoot, researchStateRoot, product, environmentName = "production" }) {
  const home = dirname(stateRoot);
  if (product === "live" && legacyTradingStatePresent(liveStateRoot))
    fail("legacy_trading_state_present", "Upgrade once so the old trading tasks are handed over before uninstalling");
  const trading = product === "live" ? await tradingdCommand(home, ["--status"]) : null;
  if (trading?.work_open) fail("trading_work_open", "Stop the running strategies before uninstalling");
  const releases = join(stateRoot, "releases");
  const executables = existsSync(releases) ? readdirSync(releases).filter((name) => /^[0-9a-f]{64}$/u.test(name)).flatMap((name) => {
    try {
      const manifest = validateManifest(JSON.parse(readFileSync(join(releases, name, "RUNTIME.json"), "utf8")), null, { enforcePlatform: false });
      return [safeDestination(join(releases, name), manifest.payload.python.executable)];
    } catch { return []; }
  }) : [];
  if (trading?.running) {
    const drained = await tradingdCommand(home, ["--drain"], 180_000);
    if (drained?.result === "failed") fail("trading_service_drain_timeout", "The trading service did not drain");
  }
  let running = false;
  let runtimeId = null;
  const registered = registeredConnection(pluginStateRoot, product);
  if (registered !== null) {
    runtimeId = registered.runtime_id;
    running = await probeConnection(join(pluginStateRoot, "connections", `${product}.json`), runtimeId);
  }
  if (running && !(await stopHost(pluginStateRoot, runtimeId, product))) fail("host_stop_failed", "verified Runtime Host must stop before uninstall");
  if (recordedHostAlive(stateRoot)) fail("host_stop_failed", "An unresponsive Host must retire before uninstall");
  // A task finishing on this Runtime (D44) still runs its interpreter.
  if (executables.length && runtimeExecutablesInUse(executables).size) fail("runtime_in_use", "A task still runs on this Runtime; wait for it or cancel it");
  const services = serviceManager(home, product, environmentName);
  for (const kind of services.kinds()) services.unregister(kind);
  for (const path of [launcherPath(home), join(stateRoot, "services.json")]) rmSync(path, { force: true });
  return withInstallLock(stateRoot, async () => {
    for (const name of ["releases", "probes", "logs"]) {
      const path = join(stateRoot, name);
      if (!existsSync(path)) continue;
      if (lstatSync(path).isSymbolicLink()) fail("runtime_path_invalid", "Runtime uninstall target is a symbolic link");
      rmSync(path, { recursive: true, force: true });
    }
    const downloads = join(dirname(stateRoot), "downloads");
    if (existsSync(downloads)) {
      if (lstatSync(downloads).isSymbolicLink()) fail("runtime_path_invalid", "Runtime downloads root is a symbolic link");
      rmSync(downloads, { recursive: true, force: true });
    }
    for (const name of ["current.json", "channel.json", "probe-failure.json", "host.lock"]) rmSync(join(stateRoot, name), { force: true });
    if (existsSync(pluginStateRoot)) {
      if (lstatSync(pluginStateRoot).isSymbolicLink()) fail("runtime_path_invalid", "plugin state root is a symbolic link");
      rmSync(pluginStateRoot, { recursive: true, force: true });
    }
    return { schema: "edgepilot-bootstrap-result-v1", uninstalled: true, preserved: [liveStateRoot, researchStateRoot] };
  });
}

function registeredConnection(pluginStateRoot, product) {
  const path = join(pluginStateRoot, "connections", `${product}.json`);
  if (!existsSync(path)) return null;
  const info = lstatSync(path);
  if (!info.isFile() || info.isSymbolicLink() || info.size > 65536) fail("connection_state_invalid", "Registered connection is invalid");
  let connection;
  try { connection = JSON.parse(readFileSync(path, "utf8")); } catch { fail("connection_state_invalid", "Registered connection is unreadable"); }
  if (!validControlConnection(connection, product)) fail("connection_state_invalid", "Registered connection must be product-bound loopback");
  return connection;
}

function recordedHostAlive(stateRoot) {
  const path = join(stateRoot, "host.lock");
  if (!existsSync(path)) return false;
  try { const owner = JSON.parse(readFileSync(path, "utf8")); return Number.isSafeInteger(owner.pid) && owner.pid > 0 && processExists(owner.pid); }
  catch { fail("host_state_invalid", "Host ownership cannot be verified"); }
}

/**
 * ``bootstrap service <status|start|restart|stop> --service host|tradingd [--force yes]``
 * (v2/11 section 2). The Host runs ``start`` and ``restart`` of the trading service
 * through the commands recorded in ``services.json`` (D43).
 */
async function serviceCommand({ action, kind, force, home, stateRoot, pluginStateRoot, liveStateRoot, researchStateRoot, product, environmentName, marketplaceOrigin }) {
  if (!["status", "start", "restart", "stop"].includes(action) || !["host", "tradingd"].includes(kind)) fail("usage", "service <status|start|restart|stop> --service host|tradingd");
  if (kind === "tradingd" && product !== "live") fail("usage", "Only Live has a trading service");
  const services = serviceManager(home, product, environmentName);
  const status = async () => ({
    mode: readServicesRecord(home)?.mode ?? null,
    registered: services.registered(kind),
    ...(kind === "tradingd" ? { tradingd: await tradingdCommand(home, ["--status"]) }
      : { host: await probeConnection(join(pluginStateRoot, "connections", `${product}.json`), (await activeRuntime(stateRoot, null)).manifest.runtime_id) }),
  });
  if (action === "status") return { schema: "edgepilot-bootstrap-result-v1", service: await status() };
  if (kind === "host") {
    const runtime = await activeRuntime(stateRoot, null);
    if (action !== "start") {
      const running = await probeConnection(join(pluginStateRoot, "connections", `${product}.json`), runtime.manifest.runtime_id);
      if (running && !(await stopHost(pluginStateRoot, runtime.manifest.runtime_id, product))) fail("host_stop_failed", "the verified Runtime Host did not stop");
    }
    if (action !== "stop") await startHost({ stateRoot, pluginStateRoot, liveStateRoot, researchStateRoot, trustedKeys: null, trustedKeyArguments: [],
      marketplaceOrigin, environmentName, selectedRuntime: runtime });
    return { schema: "edgepilot-bootstrap-result-v1", service: await status() };
  }
  if (action !== "start") {
    const drained = await tradingdCommand(home, ["--drain"], 180_000);
    if (drained?.result === "failed") {
      // An engine that does not answer cannot drain; only an explicit restart or a forced
      // stop ends it, by its command line (11 section 1.3).
      if (action === "stop" && !force) fail("trading_service_drain_timeout", "The trading service did not drain; use --force yes to stop it");
      const runtime = await activeRuntime(stateRoot, null);
      const python = safeDestination(runtime.root, runtime.manifest.payload.python.executable);
      const processes = runtimeProcessesByRole({ python, roles: ["tradingd", "engine"], birthOf: processBirth });
      await stopAuthorizedProcesses(processes, { birthOf: processBirth });
    }
  }
  if (action !== "stop") {
    const runtime = await activeRuntime(stateRoot, null);
    const liveDashboardPort = Number(process.env.EDGEPILOT_LIVE_DASHBOARD_PORT ?? 8787);
    const researchDashboardPort = Number(process.env.EDGEPILOT_RESEARCH_DASHBOARD_PORT ?? 8686);
    const hostArgs = hostArguments({ root: runtime.root, manifest: runtime.manifest, stateRoot, pluginStateRoot, liveStateRoot, researchStateRoot,
      trustedKeys: null, trustedKeyArguments: [], marketplaceOrigin, environmentName, hostPort: DEFAULT_HOST_PORT, liveDashboardPort, researchDashboardPort });
    refreshServices({ home, runtime, product, environmentName, marketplaceOrigin, liveStateRoot, researchStateRoot, hostArgs, mode: readServicesRecord(home)?.mode ?? null });
    rotateHostLog(join(stateRoot, "logs", "tradingd.log"));
    services.ensureRunning("tradingd");
    refreshServices({ home, runtime, product, environmentName, marketplaceOrigin, liveStateRoot, researchStateRoot, hostArgs, mode: services.mode });
    await waitForTradingService(home);
  }
  return { schema: "edgepilot-bootstrap-result-v1", service: await status() };
}

function trustedKeyOptions(options) {
  const values = optionMany(options, "trusted-key");
  if (values.length < 1) fail("trusted_keys_missing", "at least one trusted Runtime key is required");
  const keys = new Map();
  for (const value of values) {
    const index = value.indexOf("=");
    if (index < 1 || keys.has(value.slice(0, index))) fail("trusted_key_invalid", "trusted key must be unique KEY_ID=BASE64");
    keys.set(value.slice(0, index), strictBase64(value.slice(index + 1), 32, "trusted_key_invalid"));
  }
  return { keys, values };
}

function parseOptions(arguments_) {
  const options = new Map();
  for (let index = 0; index < arguments_.length; index += 1) {
    const key = arguments_[index];
    if (!key.startsWith("--") || index + 1 >= arguments_.length) fail("usage", "bootstrap options use --name value");
    const name = key.slice(2);
    const values = options.get(name) ?? [];
    values.push(arguments_[index + 1]);
    options.set(name, values);
    index += 1;
  }
  return options;
}

function option(options, name, fallback = undefined) {
  const values = options.get(name);
  if (!values) return fallback;
  if (values.length !== 1) fail("usage", `--${name} may be provided once`);
  return values[0];
}

function optionMany(options, name) { return options.get(name) ?? []; }

function absoluteOption(options, name, fallback) {
  const value = option(options, name, fallback);
  if (!isAbsolute(value)) fail("usage", `--${name} must be absolute`);
  return resolve(value);
}

const LIFECYCLE_COMMANDS = ["ensure-start", "start", "update", "repair", "status", "stop", "restart", "service", "uninstall", "gc", "doctor"];
const SERVICE_ACTIONS = ["status", "start", "restart", "stop"];

// ``service <action> --name value ...``: the action is the only positional argument.
function commandOptions(arguments_) {
  if (arguments_[0] !== "service") return parseOptions(arguments_.slice(1));
  if (!SERVICE_ACTIONS.includes(arguments_[1])) fail("usage", "service <status|start|restart|stop> --service host|tradingd");
  return parseOptions(arguments_.slice(2));
}

export async function cli(arguments_) {
  const command = arguments_[0];
  // Reject unknown commands before any lock or state directory is created.
  if (!LIFECYCLE_COMMANDS.includes(command)) fail("usage", "Unknown Runtime lifecycle command");
  const options = commandOptions(arguments_);
  const product = option(options, "product", null);
  if (!["live", "research"].includes(product)) fail("usage", "bootstrap requires --product live|research");
  const home = absoluteOption(options, "runtime-home", join(homedir(), `.edgepilot-runtime-${product}-production`));
  const environmentName = option(options, "environment", "production");
  validateEnvironmentIsolation({ product, environmentName, marketplaceOrigin: option(options, "marketplace-origin", product === "live" ? (environmentName === "local" ? LOCAL_MARKETPLACE_ORIGIN : PRODUCTION_MARKETPLACE_ORIGIN) : null), runtimeHome: home, liveStateRoot: absoluteOption(options, "live-state-root", join(homedir(), ".edgepilot")), researchStateRoot: absoluteOption(options, "research-state-root", join(homedir(), ".edgepilot-research")), liveDashboardPort: Number(process.env.EDGEPILOT_LIVE_DASHBOARD_PORT ?? 8787), researchDashboardPort: Number(process.env.EDGEPILOT_RESEARCH_DASHBOARD_PORT ?? 8686) });
  if (["status", "doctor"].includes(command) || (command === "service" && arguments_[1] === "status")) return cliUnlocked(arguments_);
  return withLifecycleLock(join(home, "runtime"), () => cliUnlocked(arguments_));
}

async function cliUnlocked(arguments_) {
  const command = arguments_[0];
  if (!LIFECYCLE_COMMANDS.includes(command)) fail("usage", "Unknown Runtime lifecycle command");
  const options = commandOptions(arguments_);
  const product = option(options, "product", null);
  if (!["live", "research"].includes(product)) fail("usage", "bootstrap requires --product live|research");
  const environmentName = option(options, "environment", "production");
  const marketplaceOrigin = option(options, "marketplace-origin", product === "live" ? (environmentName === "local" ? LOCAL_MARKETPLACE_ORIGIN : PRODUCTION_MARKETPLACE_ORIGIN) : null);
  if (!["local", "production"].includes(environmentName)) fail("usage", "--environment must be local or production");
  const home = absoluteOption(options, "runtime-home", join(homedir(), `.edgepilot-runtime-${product}-production`));
  const stateRoot = join(home, "runtime");
  const pluginStateRoot = join(home, "plugins");
  const liveStateRoot = absoluteOption(options, "live-state-root", join(homedir(), ".edgepilot"));
  const researchStateRoot = absoluteOption(options, "research-state-root", join(homedir(), ".edgepilot-research"));
  const trusted = optionMany(options, "trusted-key").length === 0 ? { keys: null, values: [] } : trustedKeyOptions(options);
  if (command === "service") {
    const force = option(options, "force", "no");
    if (!["yes", "no"].includes(force)) fail("usage", "--force takes yes or no");
    return serviceCommand({ action: arguments_[1], kind: option(options, "service", null), force: force === "yes", home, stateRoot, pluginStateRoot,
      liveStateRoot, researchStateRoot, product, environmentName, marketplaceOrigin });
  }
  if (command === "doctor") {
    const value = await doctor({ stateRoot, pluginStateRoot, liveStateRoot, trustedKeys: trusted.keys, product });
    const output = option(options, "output", null);
    if (output !== null) atomicJson(absoluteOption(options, "output"), value);
    return { ...value, diagnostic_output: output === null ? null : absoluteOption(options, "output") };
  }
  if (command === "gc") return { schema: "edgepilot-bootstrap-result-v1", gc: await garbageCollect({ stateRoot, pluginStateRoot, maximumReleases: Number(option(options, "maximum-releases", "1")), maximumBytes: Number(option(options, "maximum-bytes", String(5 * 1024 ** 3))) }) };
  if (command === "uninstall") {
    return uninstallRuntime({ stateRoot, pluginStateRoot, liveStateRoot, researchStateRoot, product, environmentName });
  }
  if (["status", "stop"].includes(command)) {
    const registered = registeredConnection(pluginStateRoot, product);
    const runtimeId = registered?.runtime_id ?? null;
    const running = runtimeId !== null && await probeConnection(join(pluginStateRoot, "connections", `${product}.json`), runtimeId);
    const stopped = command === "stop" && running ? await stopHost(pluginStateRoot, runtimeId, product) : false;
    if (command === "stop" && ((running && !stopped) || recordedHostAlive(stateRoot))) fail("host_stop_failed", "Host retirement is not verified");
    return { schema: "edgepilot-bootstrap-result-v1", runtime_id: runtimeId, running: command === "stop" ? false : running, stopped,
      lifecycle: readLifecycleState(stateRoot), plugin_state_root: pluginStateRoot };
  }
  if (["restart", "start"].includes(command)) {
    const pending = readLifecycleState(stateRoot);
    const incomplete = pending?.cutover_started && pending.phase !== "ready";
    if (incomplete) fail("runtime_repair_required", "Continue the bound upgrade before starting a Runtime");
    const active = incomplete && pending.target_runtime_id ? await runtimeById(stateRoot, pending.target_runtime_id, trusted.keys) : await activeRuntime(stateRoot, trusted.keys);
    if (manifestProduct(active.manifest) !== product) fail("runtime_product_incompatible", "installed Runtime belongs to another product");
    const running = await probeConnection(join(pluginStateRoot, "connections", `${product}.json`), active.manifest.runtime_id);
    const shouldStop = command === "restart" && running;
    const stopped = shouldStop ? await stopHost(pluginStateRoot, active.manifest.runtime_id, product) : false;
    if (shouldStop && !stopped) fail("host_stop_failed", "the verified Runtime Host did not stop");
    const started = await startHost({ stateRoot, pluginStateRoot, liveStateRoot, researchStateRoot, trustedKeys: trusted.keys, trustedKeyArguments: trusted.values, marketplaceOrigin, environmentName, hostPort: Number(option(options, "host-port", String(DEFAULT_HOST_PORT))) });
    return { schema: "edgepilot-bootstrap-result-v1", runtime_id: started.runtimeId, running: true, stopped, host: started, plugin_state_root: pluginStateRoot };
  }
  if (["ensure-start", "update", "repair"].includes(command)) {
    const channelUrl = option(options, "channel-url", null);
    const expectedProductVersion = option(options, "expected-product-version", option(options, "plugin-version", "").split("+", 1)[0] || null);
    const expectedRuntimeIds = optionMany(options, "expected-runtime-id");
    const switchAction = option(options, "switch-action", null);
    const choice = switchAction === null ? null : { action: switchAction,
      operation_id: option(options, "operation-id"), snapshot_digest: option(options, "snapshot-digest") };
    if (choice && (!["defer", "stop_and_continue"].includes(choice.action) || !/^[0-9a-f-]{36}$/.test(choice.operation_id)
        || !isDigest(choice.snapshot_digest))) fail("usage", "Invalid Runtime switch selection");

    if ((expectedProductVersion !== null && !isSemver(expectedProductVersion)) || expectedRuntimeIds.some((id) => !isDigest(id))) fail("usage", "plugin Runtime binding is invalid");
    if (channelUrl === null) fail("usage", `${command} requires --channel-url`);
    let active = null;
    try { active = await activeRuntime(stateRoot, null); } catch { /* missing or damaged install needs preparation */ }
    if (active !== null && manifestProduct(active.manifest) !== product) fail("runtime_product_incompatible", "installed Runtime belongs to another product");
    const pending = readLifecycleState(stateRoot);
    if (pending && expectedProductVersion !== null && compareSemver(pending.target_version, expectedProductVersion) > 0) {
      // Preparation failure never removed the old service's right to run; reuse it
      // without allowing its launcher to overwrite the newer transaction.
      const preparationOnly = !pending.cutover_started && ["prepare", "inspect"].includes(pending.interrupted_phase ?? pending.phase);
      if (command === "ensure-start" && preparationOnly && active !== null && expectedRuntimeIds.includes(active.manifest.runtime_id)) {
        const host = await startHost({ stateRoot, pluginStateRoot, liveStateRoot, researchStateRoot, trustedKeys: null, trustedKeyArguments: [], marketplaceOrigin, environmentName, selectedRuntime: active });
        return { schema: "edgepilot-bootstrap-result-v1", runtime_id: active.manifest.runtime_id, reused: true, offline: true, host, lifecycle: pending };
      }
      fail("plugin_session_stale", "An older plugin cannot replace a newer lifecycle transaction");
    }
    if (active !== null && expectedProductVersion !== null && compareSemver(active.manifest.payload.release_version, expectedProductVersion) > 0) fail("plugin_session_stale", "older plugin cannot alter the active Runtime");
    const ordinaryStart = command !== "repair" && !pending?.cutover_started;
    const currentHost = active === null ? null : await switchHostIdentity(pluginStateRoot, product);
    if (active !== null && choice?.action !== "defer" && active.manifest.payload.release_version === expectedProductVersion
        && (currentHost === null || currentHost.runtime_id === active.manifest.runtime_id)
        && expectedRuntimeIds.includes(active.manifest.runtime_id)
        && (ordinaryStart || (command !== "repair" && pending?.phase === "ready"))) {
      const host = await startHost({ stateRoot, pluginStateRoot, liveStateRoot, researchStateRoot, trustedKeys: null, trustedKeyArguments: [], marketplaceOrigin, environmentName, selectedRuntime: active });
      if (pending && pending.target_version === expectedProductVersion && pending.target_ids.includes(active.manifest.runtime_id)
          && (!pending.target_runtime_id || pending.target_runtime_id === active.manifest.runtime_id) && pending.phase !== "ready") {
        writeLifecycleState(join(stateRoot, "lifecycle.json"), { ...pending, phase: "ready", last_error: null, updated_at: new Date().toISOString() });
      }
      return { schema: "edgepilot-bootstrap-result-v1", runtime_id: active.manifest.runtime_id, reused: true, offline: true, host };
    }
    if (expectedProductVersion !== null && expectedRuntimeIds.length > 0) {
      return forwardUpgrade({ home, stateRoot, pluginStateRoot, liveStateRoot, researchStateRoot, product, environmentName, marketplaceOrigin,
        channelUrl, version: expectedProductVersion, runtimeIds: expectedRuntimeIds, repair: command === "repair", hostPort: Number(option(options, "host-port", String(DEFAULT_HOST_PORT))), choice });
    }
    fail("runtime_binding_missing", "Use the plugin's bundled lifecycle entry with its exact version and Runtime ID");
  }
  fail("runtime_entry_retired", "Use the fixed-release plugin lifecycle entry");
}

function fail(code, message) { throw new BootstrapError(code, message); }

const invokedPath = process.argv[1] ? realpathSync(resolve(process.argv[1])) : null;
if (invokedPath === realpathSync(fileURLToPath(import.meta.url))) {
  const startedAt = Date.now();
  cli(process.argv.slice(2)).then(
    (result) => process.stdout.write(`${canonical({ ...result, duration_ms: Date.now() - startedAt })}\n`),
    (error) => {
      const code = /^[a-z][a-z0-9_]{0,100}$/.test(error?.code) ? error.code : "bootstrap_internal_failure";
      process.stderr.write(`EdgePilot bootstrap: ${code}\n`);
      process.exitCode = 1;
    },
  );
}
