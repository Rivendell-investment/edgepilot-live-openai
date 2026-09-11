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

async function runLiveMaintenance({ python, liveStateRoot, runtimeId, operation = "inspect", jobRef, accountRef, idempotencyKey, registrationHome, evidenceDigest, acknowledgement, env }) {
  const args = ["-I", "-B", "-m", "edgepilot.runtime_maintenance", operation, "--state-root", liveStateRoot, "--runtime-id", runtimeId];
  for (const [name, value] of [["job-ref", jobRef], ["account-ref", accountRef], ["idempotency-key", idempotencyKey], ["registration-home", registrationHome], ["evidence-digest", evidenceDigest], ["acknowledgement", acknowledgement]]) {
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
const { snapshotRuntimeProcesses, stopAuthorizedProcesses, runtimeExecutablesInUse } = (() => {
// Retire only service processes executing the verified old Runtime interpreter.
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
  if (/^(?:-(?:I|B|u)\s+)*-c\s+["']?from (?:edgepilot\.dashboard\.http import _serve_unmanaged_for_test|edgepilot_research\.ui import serve)(?:\s|;|$)/u.test(args)) return "dashboard";
  return null;
}

function runtimeProcesses(python) {
  const executable = resolve(python);
  let entries;
  if (process.platform === "win32") {
    const powershell = join(process.env.SYSTEMROOT ?? "C:\\Windows", "System32", "WindowsPowerShell", "v1.0", "powershell.exe");
    const quoted = executable.replaceAll("'", "''");
    const script = `@(Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object {$_.ExecutablePath -eq '${quoted}'} | Select-Object ProcessId,CommandLine) | ConvertTo-Json -Compress`;
    const result = spawnSync(powershell, ["-NoProfile", "-NonInteractive", "-Command", script], { encoding: "utf8", timeout: 5000, maxBuffer: 1024 * 1024, windowsHide: true });
    if (result.status !== 0) throw processFailure("runtime_process_inspection_failed");
    try {
      const value = result.stdout.trim() ? JSON.parse(result.stdout) : [];
      entries = (Array.isArray(value) ? value : [value]).map(item => ({ pid: item.ProcessId, command: item.CommandLine }));
    } catch { throw processFailure("runtime_process_inspection_failed"); }
  } else {
    const result = spawnSync("/bin/ps", ["-u", String(process.getuid()), "-o", "pid=,command="], { encoding: "utf8", timeout: 3000, maxBuffer: 4 * 1024 * 1024 });
    if (result.status !== 0) throw processFailure("runtime_process_inspection_failed");
    entries = result.stdout.split("\n").flatMap(line => {
      const match = /^\s*(\d+)\s+(.+)$/u.exec(line);
      if (!match) return [];
      const command = match[2];
      return command.startsWith(executable + " ") || command.startsWith('"' + executable + '" ') ? [{ pid: Number(match[1]), command }] : [];
    });
  }
  return entries.map(item => {
    if (!Number.isSafeInteger(item.pid) || item.pid <= 0 || typeof item.command !== "string") throw processFailure("runtime_process_identity_unverified");
    const role = classifyRuntimeCommand(executable, item.command);
    return { pid: item.pid, service: role !== null, role };
  });
}

async function retireRuntimeProcesses({ python, birthOf, allowForce = false, allowInUse = false, role = null }) {
  const snapshot = runtimeProcesses(python);
  if (!allowInUse && snapshot.some(item => !item.service)) throw processFailure("runtime_process_in_use");
  const selected = snapshot.filter(item => item.service && (role === null || item.role === role));
  for (const item of selected) {
    if (item.pid === process.pid) throw processFailure("runtime_process_identity_unverified");
    const exists = () => {
      try { process.kill(item.pid, 0); return true; }
      catch (error) { if (error.code === "ESRCH") return false; throw processFailure("runtime_process_identity_unverified"); }
    };
    if (!exists()) continue; // Parent retirement can already have reaped its children.
    const birth = birthOf(item.pid);
    if (birth === null) { if (!exists()) continue; throw processFailure("runtime_process_identity_unverified"); }
    const remains = () => {
      if (!exists()) return false;
      const current = birthOf(item.pid);
      if (current === null) { if (!exists()) return false; throw processFailure("runtime_process_identity_unverified"); }
      return current === birth;
    };
    if (!remains()) continue;
    try { process.kill(item.pid, "SIGTERM"); } catch (error) { if (error.code !== "ESRCH") throw processFailure("host_stop_failed"); }
    const deadline = Date.now() + 5000;
    while (remains() && Date.now() < deadline) await new Promise(accept => setTimeout(accept, 50));
    if (remains() && allowForce) process.kill(item.pid, "SIGKILL");
    const forcedDeadline = Date.now() + 2000;
    while (remains() && Date.now() < forcedDeadline) await new Promise(accept => setTimeout(accept, 50));
    if (remains()) throw processFailure("host_stop_failed");
  }
  const remaining = runtimeProcesses(python);
  if (remaining.some(item => (item.service && (role === null || item.role === role)) || (!allowInUse && !item.service))) throw processFailure("runtime_process_in_use");
  return { retired: selected.length };
}

function processInventory() {
  if (process.platform === "win32") {
    const powershell = join(process.env.SYSTEMROOT ?? "C:\\Windows", "System32", "WindowsPowerShell", "v1.0", "powershell.exe");
    const script = "$me=[System.Security.Principal.WindowsIdentity]::GetCurrent().Name; @(Get-CimInstance Win32_Process -ErrorAction Stop | ForEach-Object {$o=Invoke-CimMethod -InputObject $_ -MethodName GetOwner -ErrorAction Stop; if (($o.Domain+'\\'+$o.User) -eq $me) {$_ | Select-Object ProcessId,ParentProcessId,CommandLine}}) | ConvertTo-Json -Compress";
    const result = spawnSync(powershell, ["-NoProfile", "-NonInteractive", "-Command", script], { encoding: "utf8", timeout: 120000, maxBuffer: 4 * 1024 * 1024, windowsHide: true });
    if (result.status !== 0) throw processFailure("runtime_process_inspection_failed");
    try { const value = JSON.parse(result.stdout || "[]"); return (Array.isArray(value) ? value : [value]).map(item => ({ pid: item.ProcessId, parent: item.ParentProcessId, command: item.CommandLine ?? "" })); }
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
  const entries = processInventory();
  return new Set(executables.filter(executable => entries.some(entry =>
    entry.command.startsWith(executable + " ") || entry.command.startsWith('"' + executable + '" '))));
}

function snapshotRuntimeProcesses({ python, hostPid = null, birthOf, authorized = [], inventory = processInventory }) {
  const executable = resolve(python), entries = inventory();
  const children = new Map();
  for (const entry of entries) { const rows = children.get(entry.parent) ?? []; rows.push(entry); children.set(entry.parent, rows); }
  const selected = new Map(), queue = [];
  for (const entry of entries) {
    const exact = entry.command.startsWith(executable + " ") || entry.command.startsWith('"' + executable + '" ');
    const approved = authorized.some(old => old.pid === entry.pid && old.birth === birthOf(entry.pid));
    if (entry.pid === hostPid || exact || approved) { selected.set(entry.pid, entry); queue.push(entry.pid); }
  }
  for (let index = 0; index < queue.length; index += 1) {
    for (const entry of children.get(queue[index]) ?? []) {
      if (!selected.has(entry.pid)) { selected.set(entry.pid, entry); queue.push(entry.pid); }
    }
  }
  if (selected.size > 200) throw processFailure("runtime_process_inventory_exceeded");
  return [...selected.values()].map(entry => {
    if (!Number.isSafeInteger(entry.pid) || entry.pid <= 0 || entry.pid === process.pid) throw processFailure("runtime_process_identity_unverified");
    const birth = birthOf(entry.pid);
    if (!birth) throw processFailure("runtime_process_identity_unverified");
    return { pid: entry.pid, birth, role: entry.pid === hostPid ? "host" : classifyRuntimeCommand(executable, entry.command) ?? "execution" };
  }).sort((a, b) => a.pid - b.pid);
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
return { snapshotRuntimeProcesses, stopAuthorizedProcesses, runtimeExecutablesInUse };
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
export const HOST_START_TIMEOUT_MS = 60_000;
const DEFAULT_HOST_PORT = 0;
const BOOTSTRAP_PRODUCT_VERSION = "1.2.19";
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

function livePinnedRuntimeIds(liveStateRoot) {
  const result = new Set();
  if (liveStateRoot === null) return result;
  const root = join(liveStateRoot, "runtime-live-process-jobs");
  if (!existsSync(root)) return result;
  if (lstatSync(root).isSymbolicLink() || !lstatSync(root).isDirectory()) fail("runtime_pin_state_invalid", "Live Runtime job root is invalid");
  for (const name of readdirSync(root)) {
    if (!/^job_[A-Za-z0-9_-]{20,128}\.json$/u.test(name)) continue;
    const path = join(root, name);
    const metadata = lstatSync(path);
    if (!metadata.isFile() || metadata.isSymbolicLink() || metadata.size > 1024 * 1024) fail("runtime_pin_state_invalid", "Live Runtime job record is invalid");
    let value;
    try { value = JSON.parse(readFileSync(path, "utf8")); } catch { fail("runtime_pin_state_invalid", "Live Runtime job record is invalid"); }
    if (!isDigest(value?.runtime_id)) fail("runtime_pin_state_invalid", "Live job Runtime identity is invalid");
    if (!["succeeded", "completed", "failed", "cancelled"].includes(value?.state)) result.add(value.runtime_id);
  }
  return result;
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

export async function garbageCollect({ stateRoot, liveStateRoot, pluginStateRoot = null, maximumReleases = 1, maximumBytes = 5 * 1024 ** 3, pinnedRuntimeIds = null }) {
  if (!Number.isSafeInteger(maximumReleases) || maximumReleases < 1 || !Number.isSafeInteger(maximumBytes) || maximumBytes < 1) fail("gc_policy_invalid", "Runtime GC policy is invalid");
  return withInstallLock(stateRoot, async () => {
    const releases = join(stateRoot, "releases");
    if (!existsSync(releases)) return { removed: [], retained: [], bytes: 0 };
    await recoverRepairBackups(releases);
    removeGeneratedFilesystemMetadata(releases, "runtime_release_root_invalid");
    const pointer = readPointer(join(stateRoot, "current.json"));
    const protectedIds = new Set([pointer.current_runtime_id, ...(pinnedRuntimeIds ?? livePinnedRuntimeIds(liveStateRoot))].filter(Boolean));
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

export async function installRuntime({ archivePath, manifestPath, stateRoot, trustedKeys, liveStateRoot = null, enforcePlatform = true, probe = probeRuntime, beforeActivate = null, repair = false, activate = true }) {
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
    if (previous !== null && previous.current_runtime_id !== manifest.runtime_id && liveStateRoot !== null && livePinnedRuntimeIds(liveStateRoot).has(previous.current_runtime_id)) fail("runtime_pinned", "active Runtime is pinned by a persistent job");
    if (repair && liveStateRoot !== null && livePinnedRuntimeIds(liveStateRoot).size > 0) fail("runtime_pinned", "Live jobs block Runtime repair");
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
        if (liveStateRoot !== null && livePinnedRuntimeIds(liveStateRoot).size > 0) fail("runtime_pinned", "Live jobs block Runtime replacement");
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
    if (previous?.current_runtime_id !== manifest.runtime_id && liveStateRoot !== null && livePinnedRuntimeIds(liveStateRoot).size > 0) fail("runtime_pinned", "active or unreconciled Live jobs block Runtime replacement");
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

async function inspectPreparedJobs(prepared, liveStateRoot, operation = "reconcile", options = {}) {
  if (operation !== "retire-legacy" && !existsSync(join(liveStateRoot, "runtime-live-process-jobs"))) return { schema: "edgepilot-live-maintenance-v1", jobs: [], pinned_runtime_ids: [] };
  return runLiveMaintenance({ python: safeDestination(prepared.runtimeRoot, prepared.manifest.payload.python.executable), liveStateRoot,
    runtimeId: prepared.manifest.runtime_id, operation, env: cleanHostEnvironment(), ...options });
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

async function commitRuntimeSwitch({ transaction, prepared, started, stateRoot, pluginStateRoot, liveStateRoot, product, pins }) {
  transaction.advance("commit");
  atomicJson(join(stateRoot, "current.json"), { schema: "edgepilot-runtime-pointer-v1", current_runtime_id: prepared.manifest.runtime_id, previous_runtime_id: null });
  transaction.advance("ready", { blockers: [], last_error: null });
  try {
    await garbageCollect({ stateRoot, liveStateRoot: product === "live" ? liveStateRoot : null, pluginStateRoot,
      pinnedRuntimeIds: pins, maximumReleases: 1 });
    if (transaction.value.retired_directory) rmSync(join(stateRoot, "retired", transaction.value.retired_directory), { recursive: true, force: true });
  } catch { transaction.advance("ready", { cleanup_pending: true }); }
  return { schema: "edgepilot-bootstrap-result-v1", runtime_id: prepared.manifest.runtime_id, reused: prepared.reused, offline: prepared.reused, host: started, lifecycle: transaction.value };
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
    let jobs = product === "live" ? await inspectPreparedJobs(prepared, liveStateRoot) : { jobs: [], pinned_runtime_ids: [] };
    if (jobs.truncated) fail("runtime_process_inventory_exceeded", "Too many active tasks to confirm in one switch");
    const identity = await switchHostIdentity(pluginStateRoot, product);
    if (identity?.runtime_id === prepared.manifest.runtime_id && previous?.cutover_started
        && ["start", "commit"].includes(previous.interrupted_phase ?? previous.phase)
        && prepared.runtimeRoot === join(stateRoot, "releases", runtimeDirectory(prepared.manifest.runtime_id))) {
      const started = await startHost({ stateRoot, pluginStateRoot, liveStateRoot, researchStateRoot, trustedKeys: null, trustedKeyArguments: [], marketplaceOrigin, environmentName, hostPort,
        selectedRuntime: { root: prepared.runtimeRoot, manifest: prepared.manifest } });
      return commitRuntimeSwitch({ transaction, prepared, started, stateRoot, pluginStateRoot, liveStateRoot, product, pins: jobs.pinned_runtime_ids });
    }
    let previousId = identity?.runtime_id ?? null;
    if (!previousId) { try { previousId = readPointer(join(stateRoot, "current.json")).current_runtime_id; } catch {} }
    oldRuntimeId = previousId;
    let oldPython = null;
    if (previousId) {
      const previousRoot = join(stateRoot, "releases", runtimeDirectory(previousId));
      const manifestPath = existsSync(join(previousRoot, "RUNTIME.json")) ? join(previousRoot, "RUNTIME.json")
        : transaction.value.retired_directory ? join(stateRoot, "retired", transaction.value.retired_directory, "RUNTIME.json") : null;
      if (!manifestPath) fail("runtime_identity_invalid", "Previous Runtime manifest is missing");
      const manifest = validateManifest(JSON.parse(readFileSync(manifestPath, "utf8")), null);
      if (manifest.runtime_id !== previousId || manifestProduct(manifest) !== product) fail("runtime_identity_invalid", "Previous Runtime identity differs");
      oldPython = safeDestination(previousRoot, manifest.payload.python.executable);
    }
    const inspect = async () => {
      const live = product === "live" ? await inspectPreparedJobs(prepared, liveStateRoot) : { jobs: [], pinned_runtime_ids: [] };
      const unverified = live.jobs.filter(job => job.runtime_in_use && (job.runtime_id !== previousId || job.process_evidence !== "running"));
      if (live.truncated || unverified.length) {
        transaction.advance("inspect", { blockers: unverified.map(job => ({ job_ref: job.job_ref, runtime_id: job.runtime_id, process_evidence: job.process_evidence })) });
        fail("runtime_process_identity_unverified", "An old task process cannot be verified");
      }
      const processes = oldPython ? snapshotRuntimeProcesses({ python: oldPython, hostPid: identity?.pid ?? null,
        birthOf: processBirth, authorized: transaction.value.authorized?.processes ?? [] }) : [];
      for (const job of live.jobs.filter(job => job.runtime_in_use)) {
        const birth = processBirth(job.pid);
        if (!birth) fail("runtime_process_identity_unverified", "An execution process cannot be verified");
        if (!processes.some(item => item.pid === job.pid)) processes.push({ pid: job.pid, birth, role: "execution" });
      }
      const ordinary = [];
      const directory = join(productState, "runtime-jobs");
      if (existsSync(directory)) {
        if (lstatSync(directory).isSymbolicLink()) fail("runtime_job_state_invalid", "Job store is invalid");
        for (const name of readdirSync(directory).filter(name => /^job_[A-Za-z0-9_-]+\.json$/.test(name))) {
          const path = join(directory, name);
          if (lstatSync(path).isSymbolicLink() || lstatSync(path).size > 1024 * 1024) fail("runtime_job_state_invalid", "Job record is invalid");
          const job = JSON.parse(readFileSync(path, "utf8"));
          if (identity && ["queued", "running", "cancelling"].includes(job.state)) ordinary.push({ job_ref: job.job_ref ?? name.slice(0, -5), runtime_id: job.runtime_id ?? previousId, kind: job.kind ?? "background" });
        }
      }
      return { processes, jobs: [...ordinary, ...live.jobs.filter(job => job.runtime_in_use).map(job => ({ job_ref: job.job_ref, account_ref: job.account_ref, runtime_id: job.runtime_id, kind: job.kind }))], live };
    };
    let snapshot = await inspect();
    if (snapshot.jobs.length > 200) fail("runtime_process_inventory_exceeded", "Too many tasks to confirm in one switch");
    let authorized = transaction.value.authorized ?? (choice?.action === "stop_and_continue" ? previous.selection : null);
    // Local development Hosts are detached from the launcher so an interrupted
    // terminal/IDE session can orphan them under launchd. If there are no
    // active jobs, the process snapshot is safe to reclaim automatically; do
    // not turn an idle local restart into a user confirmation gate.
    if (!authorized && environmentName === "local" && snapshot.jobs.length === 0 && snapshot.processes.length > 0) {
      switchSelection(transaction, { product, environment: environmentName, runtimeId: previousId, ...snapshot });
      authorized = transaction.value.selection;
    }
    if ((snapshot.processes.length || snapshot.jobs.length) && !selectionCovers(authorized, snapshot)) {
      switchSelection(transaction, { product, environment: environmentName, runtimeId: previousId, ...snapshot });
      return switchResult(transaction, "awaiting_confirmation");
    }
    if (authorized) transaction.advance("quiesce", { authorized });
    if (identity && (snapshot.processes.length || snapshot.jobs.length)) {
      const admission = await controlHost(pluginStateRoot, previousId, product, "quiesce");
      quiesced = admission !== null;
      if (admission === null && snapshot.jobs.length) fail("host_quiesce_failed", "The old Host cannot close task admission");
      if (admission?.blockers.some(item => item.code !== "job_active")) fail("host_quiesce_failed", "Requests or invalid task state prevent switching");
      snapshot = await inspect();
      if (!selectionCovers(authorized, snapshot)) {
        if (quiesced) await controlHost(pluginStateRoot, previousId, product, "resume");
        quiesced = false;
        switchSelection(transaction, { product, environment: environmentName, runtimeId: previousId, ...snapshot });
        return switchResult(transaction, "awaiting_confirmation");
      }
    }
    // Persist authorization before effects so an interrupted confirmed switch can resume.
    if (snapshot.processes.length || snapshot.jobs.length) {
      transaction.advance("retire", { cutover_started: true, authorized });
      for (const job of snapshot.live.jobs.filter(job => job.runtime_in_use)) {
        await inspectPreparedJobs(prepared, liveStateRoot, "stop", { jobRef: job.job_ref, accountRef: job.account_ref,
          idempotencyKey: `runtime-switch-${transaction.value.operation_id}-${job.job_ref}` });
      }
      const beforeHostStop = await inspect();
      const currentHost = await switchHostIdentity(pluginStateRoot, product);
      if (!selectionCovers(authorized, beforeHostStop)
          || (currentHost && !authorized.processes.some(item => item.pid === currentHost.pid && item.birth === processBirth(currentHost.pid)))) {
        if (quiesced) await controlHost(pluginStateRoot, previousId, product, "resume");
        quiesced = false;
        switchSelection(transaction, { product, environment: environmentName, runtimeId: previousId, ...beforeHostStop });
        return switchResult(transaction, "awaiting_confirmation");
      }
      // Host shutdown requests normal worker/Dashboard teardown; exact process identities
      // still get checked before bounded escalation if the owner does not finish.
      if (identity) await stopHost(pluginStateRoot, previousId, product);
      await stopAuthorizedProcesses(snapshot.processes, { birthOf: processBirth });
      const remaining = await inspect();
      if (remaining.processes.length || remaining.live.pinned_runtime_ids.length)
        fail("runtime_process_in_use", "A process survived the confirmed Runtime switch");
    }
    jobs = product === "live" ? await inspectPreparedJobs(prepared, liveStateRoot) : jobs;
    const final = join(stateRoot, "releases", runtimeDirectory(prepared.manifest.runtime_id));
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
    transaction.advance("start", { cutover_started: true });
    const started = await startHost({ stateRoot, pluginStateRoot, liveStateRoot, researchStateRoot, trustedKeys: null, trustedKeyArguments: [], marketplaceOrigin, environmentName, hostPort,
      selectedRuntime: { root: final, manifest: prepared.manifest } });
    return await commitRuntimeSwitch({ transaction, prepared, started, stateRoot, pluginStateRoot, liveStateRoot, product, pins: jobs.pinned_runtime_ids });
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
    "from edgepilot_runtime_host.release.probe import RuntimeWorkerProbe",
    "after_import=runtime_inventory()",
    "(_ for _ in ()).throw(RuntimeError(f'Runtime import mutated tree dont_write={sys.dont_write_bytecode} extra={sorted(after_import-expected)[:10]}')) if after_import!=expected else None",
    "manifest=RuntimeManifestEnvelope.from_dict(raw)",
    "print('runtime_probe_stage=verify_tree',flush=True)",
    "RuntimeArtifactVerifier(RuntimePlatform.current()).verify_tree(root,manifest,allow_installed_manifest=True)",
    "print('runtime_probe_stage=worker_probe',flush=True)",
    "RuntimeWorkerProbe(pathlib.Path(sys.argv[2]),timeout=10.0)(root,manifest)",
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
        reject(new BootstrapError("runtime_probe_timeout", "Runtime worker probe timed out"));
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
          reject(new BootstrapError("runtime_probe_failed", "Runtime worker probe failed"));
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

export async function installFromFunctionalChannel({ home, stateRoot, channelUrl, expectedProduct, expectedProductVersion = null, expectedRuntimeIds = [], liveStateRoot = null, beforeActivate = null, enforcePlatform = true, probe = probeRuntime, repair = false }) {
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
    const installed = await installRuntime({ archivePath, manifestPath, stateRoot, trustedKeys: null, liveStateRoot, beforeActivate, enforcePlatform, probe, repair });
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
    const installed = await installRuntime({ archivePath, manifestPath, stateRoot, trustedKeys: config.keys, liveStateRoot: config.liveStateRoot ?? null, enforcePlatform, probe });
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

export async function activatePreviousRuntime() {
  fail("runtime_rollback_retired", "Runtime upgrades are forward-only; repair the selected release");
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

export async function stopStaleHost(pluginStateRoot, expectedRuntimeId, product) {
  const connectionPath = join(pluginStateRoot, "connections", `${product}.json`);
  let connection;
  try {
    connection = JSON.parse(await readFile(connectionPath, "utf8"));
  } catch {
    return false;
  }
  if (connection.runtime_id === expectedRuntimeId && await probeConnection(connectionPath, expectedRuntimeId)) return false;
  const reachable = await probeConnection(connectionPath, connection.runtime_id);
  const stopped = await stopHost(pluginStateRoot, connection.runtime_id, product);
  if (reachable && !stopped) fail("host_stop_failed", "old Runtime Host did not stop");
  return stopped;
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

export async function startHost({ stateRoot, pluginStateRoot, liveStateRoot, researchStateRoot, trustedKeys, trustedKeyArguments, marketplaceOrigin = null, environmentName = "production", hostPort = DEFAULT_HOST_PORT, selectedRuntime = null }) {
  const { root, manifest } = selectedRuntime ?? await activeRuntime(stateRoot, trustedKeys);
  const product = manifestProduct(manifest);
  const liveDashboardPort = Number(process.env.EDGEPILOT_LIVE_DASHBOARD_PORT ?? 8787);
  const researchDashboardPort = Number(process.env.EDGEPILOT_RESEARCH_DASHBOARD_PORT ?? 8686);
  validateEnvironmentIsolation({
    product,
    environmentName,
    marketplaceOrigin,
    runtimeHome: dirname(stateRoot),
    liveStateRoot,
    researchStateRoot,
    liveDashboardPort,
    researchDashboardPort,
  });
  const connections = join(pluginStateRoot, "connections");
  if (await probeConnection(join(connections, `${product}.json`), manifest.runtime_id)) {
    const dashboard = await startDashboard(pluginStateRoot, manifest.runtime_id, product);
    return { alreadyRunning: true, runtimeId: manifest.runtime_id, dashboard };
  }
  if (await switchHostIdentity(pluginStateRoot, product))
    fail("runtime_switch_confirmation_required", "A different Host must be confirmed before replacement");
  mkdirSync(join(stateRoot, "logs"), { recursive: true, mode: 0o700 });
  const logPath = join(stateRoot, "logs", "host.log");
  rotateHostLog(logPath);
  const log = openSync(logPath, constants.O_WRONLY | constants.O_CREAT | constants.O_APPEND, 0o600);
  const python = safeDestination(root, manifest.payload.python.executable);
  const hostArguments = [
    "-I", "-B", "-m", "edgepilot_runtime_host.host_main",
    "--runtime-state-root", stateRoot,
    "--runtime-id", manifest.runtime_id,
    "--plugin-state-root", pluginStateRoot,
    "--research-state-root", researchStateRoot,
    "--live-state-root", liveStateRoot,
    "--research-plugin-template", join(root, "plugins", "edgepilot-research"),
    "--live-plugin-template", join(root, "plugins", "edgepilot-live"),
    "--host-port", String(hostPort),
  ];
  if (product === "live" && !marketplaceOrigin) fail("marketplace_origin_missing", "Live Runtime requires --marketplace-origin");
  hostArguments.push("--environment", environmentName);
  if (marketplaceOrigin) hostArguments.push("--marketplace-origin", marketplaceOrigin);
  hostArguments.push("--live-dashboard-port", String(liveDashboardPort));
  hostArguments.push("--research-dashboard-port", String(researchDashboardPort));
  if (trustedKeys === null) hostArguments.push("--functional-unsigned");
  for (const value of trustedKeyArguments) hostArguments.push("--trusted-key", value);
  const child = spawn(python, hostArguments, { detached: true, stdio: ["ignore", log, log], windowsHide: true,
    env: { ...cleanHostEnvironment(), EDGEPILOT_BOOTSTRAP_OWNS_CUTOVER: "1" } });
  closeSync(log);
  let spawnError = null;
  child.once("error", (error) => { spawnError = error; });
  const deadline = Date.now() + HOST_START_TIMEOUT_MS;
  while (Date.now() < deadline) {
    if (spawnError !== null) fail("host_start_failed", "Runtime Host process could not start");
    if (await probeConnection(join(connections, `${product}.json`), manifest.runtime_id)) {
      let dashboard;
      try { dashboard = await startDashboard(pluginStateRoot, manifest.runtime_id, product); } catch (error) { await stopChild(child); throw error; }
      child.unref();
      return { alreadyRunning: child.exitCode !== null, runtimeId: manifest.runtime_id, dashboard, ...(child.exitCode === null ? { pid: child.pid } : {}) };
    }
    if (child.exitCode !== null) {
      await new Promise((accept) => setTimeout(accept, 100));
      if (await probeConnection(join(connections, `${product}.json`), manifest.runtime_id)) { const dashboard = await startDashboard(pluginStateRoot, manifest.runtime_id, product); return { alreadyRunning: true, runtimeId: manifest.runtime_id, dashboard }; }
      continue;
    }
    await new Promise((accept) => setTimeout(accept, 100));
  }
  await stopChild(child);
  fail("host_start_timeout", "Runtime Host did not become ready");
}

export async function startDashboard(pluginStateRoot, runtimeId, product) {
  const connectionPath = join(pluginStateRoot, "connections", `${product}.json`);
  if (!await probeConnection(connectionPath, runtimeId)) fail("host_not_ready", "target Host identity differs");
  const connection = JSON.parse(await readFile(connectionPath, "utf8"));
  if (!validControlConnection(connection, product)) fail("connection_state_invalid", "Dashboard connection must be loopback and product-bound");
  const authority = JSON.parse(await readFile(join(pluginStateRoot, "connection-authority.json"), "utf8"));
  const endpoint = new URL(connection.endpoint);
  endpoint.pathname = `/peer/${product}`;
  let response;
  try { response = await fetch(endpoint, {
    // Includes listener inspection (5s), readiness (10s), and bounded child cleanup.
    // Host Dashboard readiness is bounded at 60 seconds on cold Windows starts;
    // leave request headroom so Bootstrap does not abort the peer call first.
    method: "POST", redirect: "error", signal: AbortSignal.timeout(90_000),
    headers: { Authorization: ["Bearer", authority[`${product}_app`]].join(" "), "Content-Type": "application/json" },
    body: JSON.stringify({ method: "dashboard.start" }),
  }); } catch (error) {
    fail(error?.name === "TimeoutError" ? "dashboard_request_timeout" : "dashboard_request_failed", "Dashboard lifecycle request failed");
  }
  let body;
  try { body = await response.json(); } catch { fail("dashboard_response_invalid", "Dashboard lifecycle response is invalid"); }
  const ownerErrors = new Set(["dashboard_start_failed", "dashboard_start_timeout", "dashboard_identity_mismatch", "dashboard_handoff_failed", "dashboard_port_inspection_failed", "dashboard_port_stop_failed"]);
  if (!response.ok && ownerErrors.has(body?.error?.code)) fail(body.error.code, "Dashboard owner rejected startup; inspect runtime/logs/dashboard-<product>-startup.json");
  if (!response.ok || body?.result?.running !== true || body.result.runtime_id !== runtimeId || body.result.profile !== product) fail("dashboard_not_ready", "target Dashboard did not become ready");
  if (!await probeConnection(connectionPath, runtimeId)) fail("host_not_ready", "Runtime changed during Dashboard startup");
  return body.result;
}

async function stopChild(child) {
  if (child.exitCode !== null) return;
  try { child.kill("SIGTERM"); } catch { return; }
  const graceful = await waitChild(child, 5_000);
  if (graceful || child.exitCode !== null) return;
  try { child.kill("SIGKILL"); } catch { return; }
  await waitChild(child, 2_000);
}

function waitChild(child, milliseconds) {
  if (child.exitCode !== null || child.signalCode !== null) return Promise.resolve(true);
  return new Promise((accept) => {
    const exited = () => { clearTimeout(timer); accept(true); };
    const timer = setTimeout(() => { child.off("exit", exited); accept(false); }, milliseconds);
    child.once("exit", exited);
  });
}

function cleanHostEnvironment() {
  const allowed = new Set(["SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "LANG", "LC_ALL"]);
  return {
    ...Object.fromEntries(Object.entries(process.env).filter(([key]) => allowed.has(key.toUpperCase()))),
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
        if (value.runtime_id === runtimeId) host = { pid: value.pid, running: true, started_at: value.started_at, endpoint: `${endpoint.origin}`, workers: value.workers };
      }
    } catch { /* the process/connection summary above remains authoritative */ }
  }
  const logs = join(stateRoot, "logs");
  const logBytes = existsSync(logs) ? directoryBytes(logs) : 0;
  const pins = product === "live" ? [...await reconciledPins(stateRoot, liveStateRoot)].sort() : [];
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
    workers: profiles,
    active_pinned_runtime_ids: pins,
    lifecycle: readLifecycleState(stateRoot),
    recent_error_code: recentError,
    logs: { directory: "runtime/logs", bytes: logBytes, maximum_file_bytes: 10 * 1024 * 1024, retained_files: 3 },
  };
}

async function reconciledPins(stateRoot, liveStateRoot) {
  const pins = livePinnedRuntimeIds(liveStateRoot);
  if (!existsSync(join(liveStateRoot, "runtime-live-process-jobs"))) return pins;
  let active;
  try { active = await activeRuntime(stateRoot, null); }
  catch {
    const pending = readLifecycleState(stateRoot);
    if (!pending?.target_runtime_id) return pins;
    try { active = await runtimeById(stateRoot, pending.target_runtime_id, null); }
    catch {
      try { active = await runtimeById(join(stateRoot, "prepared"), pending.target_runtime_id, null); }
      catch { return pins; }
    }
  }
  if (!active.manifest.payload.files.some(file => file.path.endsWith("/edgepilot/runtime_maintenance.py"))) return pins;
  const result = await inspectPreparedJobs({ runtimeRoot: active.root, manifest: active.manifest }, liveStateRoot, "inspect");
  return new Set(result.pinned_runtime_ids);
}

export async function uninstallRuntime({ stateRoot, pluginStateRoot, liveStateRoot, researchStateRoot, product }) {
  const pins = product === "live" ? await reconciledPins(stateRoot, liveStateRoot) : new Set();
  if (pins.size > 0) fail("runtime_pinned", "Runtime uninstall is blocked by active persistent jobs");
  let running = false;
  let runtimeId = null;
  const registered = registeredConnection(pluginStateRoot, product);
  if (registered !== null) {
    runtimeId = registered.runtime_id;
    running = await probeConnection(join(pluginStateRoot, "connections", `${product}.json`), runtimeId);
  }
  if (running && !(await stopHost(pluginStateRoot, runtimeId, product))) fail("host_stop_failed", "verified Runtime Host must stop before uninstall");
  if (recordedHostAlive(stateRoot)) fail("host_stop_failed", "An unresponsive Host must retire before uninstall");
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

export async function cli(arguments_) {
  const command = arguments_[0];
  if (command === "rollback") fail("runtime_rollback_retired", "Runtime upgrades are forward-only; repair the selected release");
  if (["install", "install-start"].includes(command)) fail("runtime_entry_retired", "Use the fixed-release plugin lifecycle entry");
  const options = parseOptions(arguments_.slice(1));
  const product = option(options, "product", null);
  if (!["live", "research"].includes(product)) fail("usage", "bootstrap requires --product live|research");
  const home = absoluteOption(options, "runtime-home", join(homedir(), `.edgepilot-runtime-${product}-production`));
  const environmentName = option(options, "environment", "production");
  validateEnvironmentIsolation({ product, environmentName, marketplaceOrigin: option(options, "marketplace-origin", product === "live" ? (environmentName === "local" ? LOCAL_MARKETPLACE_ORIGIN : PRODUCTION_MARKETPLACE_ORIGIN) : null), runtimeHome: home, liveStateRoot: absoluteOption(options, "live-state-root", join(homedir(), ".edgepilot")), researchStateRoot: absoluteOption(options, "research-state-root", join(homedir(), ".edgepilot-research")), liveDashboardPort: Number(process.env.EDGEPILOT_LIVE_DASHBOARD_PORT ?? 8787), researchDashboardPort: Number(process.env.EDGEPILOT_RESEARCH_DASHBOARD_PORT ?? 8686) });
  if (["status", "doctor"].includes(command)) return cliUnlocked(arguments_);
  return withLifecycleLock(join(home, "runtime"), () => cliUnlocked(arguments_));
}

async function cliUnlocked(arguments_) {
  const command = arguments_[0];
  if (command === "rollback") fail("runtime_rollback_retired", "Runtime upgrades are forward-only; repair the selected release");
  if (!["ensure-start", "start", "update", "repair", "status", "stop", "restart", "runtime-blockers", "stop-job", "review-job", "uninstall", "gc", "doctor"].includes(command)) fail("usage", "Unknown Runtime lifecycle operation");
  const options = parseOptions(arguments_.slice(1));
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
  if (["runtime-blockers", "stop-job", "review-job"].includes(command)) {
    if (product !== "live") fail("maintenance_operation_invalid", "Live management is unavailable for Research");
    const prepared = await prepareBoundRuntime({ home, stateRoot, channelUrl: option(options, "channel-url"), product,
      version: option(options, "expected-product-version"), runtimeIds: optionMany(options, "expected-runtime-id") });
    return inspectPreparedJobs(prepared, liveStateRoot, command === "stop-job" ? "stop" : command === "review-job" ? "review" : "inspect", command !== "runtime-blockers" ? {
      jobRef: option(options, "job-ref"), accountRef: option(options, "account-ref"), idempotencyKey: option(options, "idempotency-key"),
      evidenceDigest: option(options, "evidence-digest"), acknowledgement: option(options, "acknowledgement"),
    } : {});
  }
  if (command === "doctor") {
    const value = await doctor({ stateRoot, pluginStateRoot, liveStateRoot, trustedKeys: trusted.keys, product });
    const output = option(options, "output", null);
    if (output !== null) atomicJson(absoluteOption(options, "output"), value);
    return { ...value, diagnostic_output: output === null ? null : absoluteOption(options, "output") };
  }
  if (command === "gc") return { schema: "edgepilot-bootstrap-result-v1", gc: await garbageCollect({ stateRoot, liveStateRoot: product === "live" ? liveStateRoot : null, pluginStateRoot, pinnedRuntimeIds: product === "live" ? await reconciledPins(stateRoot, liveStateRoot) : [], maximumReleases: Number(option(options, "maximum-releases", "1")), maximumBytes: Number(option(options, "maximum-bytes", String(5 * 1024 ** 3))) }) };
  if (command === "uninstall") {
    return uninstallRuntime({ stateRoot, pluginStateRoot, liveStateRoot, researchStateRoot, product });
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
    if (incomplete && ["start", "restart"].includes(command)) fail("runtime_repair_required", "Continue the bound upgrade before starting a Runtime");
    const active = incomplete && pending.target_runtime_id ? await runtimeById(stateRoot, pending.target_runtime_id, trusted.keys) : await activeRuntime(stateRoot, trusted.keys);
    if (manifestProduct(active.manifest) !== product) fail("runtime_product_incompatible", "installed Runtime belongs to another product");
    const running = await probeConnection(join(pluginStateRoot, "connections", `${product}.json`), active.manifest.runtime_id);
    const shouldStop = ["stop", "restart"].includes(command) && running;
    const stopped = shouldStop ? await stopHost(pluginStateRoot, active.manifest.runtime_id, product) : false;
    if (shouldStop && !stopped) fail("host_stop_failed", "the verified Runtime Host did not stop");
    if (command === "status" || command === "stop") {
      return { schema: "edgepilot-bootstrap-result-v1", runtime_id: active.manifest.runtime_id, running, stopped, plugin_state_root: pluginStateRoot };
    }
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
    const resumeTarget = pending?.cutover_started && ["start", "commit"].includes(pending.interrupted_phase ?? pending.phase)
      && pending.target_runtime_id === active?.manifest.runtime_id;
    const ordinaryStart = command !== "repair" && !pending?.cutover_started;
    const currentHost = active === null ? null : await switchHostIdentity(pluginStateRoot, product);
    if (active !== null && choice?.action !== "defer" && active.manifest.payload.release_version === expectedProductVersion
        && (currentHost === null || currentHost.runtime_id === active.manifest.runtime_id)
        && expectedRuntimeIds.includes(active.manifest.runtime_id)
        && (ordinaryStart || resumeTarget || (command !== "repair" && pending?.phase === "ready"))) {
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
