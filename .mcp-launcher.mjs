import { spawn, spawnSync } from "node:child_process";
import { resolve } from "node:path";

const moduleName = process.argv[2];
const sourceRoot = process.argv[3];
if (!moduleName || !sourceRoot) {
  process.stderr.write("EdgePilot MCP launcher requires a Python module and source root.\n");
  process.exit(2);
}

const env = { ...process.env };
for (const name of [
  "PYTHONHOME",
  "PYTHONPATH",
  "EDGEPILOT_HOME",
  "EDGEPILOT_MARKETPLACE_ORIGIN",
  "EDGEPILOT_PYTHON",
  "EDGEPILOT_VENV",
]) {
  delete env[name];
}
env.PYTHONPATH = resolve(process.cwd(), sourceRoot);
const candidates = process.platform === "win32"
  ? [["py", ["-3"], ["-3", "-m", moduleName]], ["python", [], ["-m", moduleName]], ["python3", [], ["-m", moduleName]]]
  : [["python3", [], ["-m", moduleName]], ["python", [], ["-m", moduleName]]];
let activeChild;

for (const signal of ["SIGINT", "SIGTERM"]) {
  process.on(signal, () => activeChild?.kill(signal));
}

function start(index) {
  if (index >= candidates.length) {
    process.stderr.write("EdgePilot requires Python 3 on PATH to start its local MCP.\n");
    process.exit(127);
  }
  const [command, prefix, args] = candidates[index];
  const probe = spawnSync(command, [...prefix, "-c", "import sys; raise SystemExit(0 if (3,12) <= sys.version_info[:2] < (3,15) else 2)"], {
    cwd: process.cwd(), env, stdio: "ignore",
  });
  if (probe.error?.code === "ENOENT" || probe.status !== 0) {
    start(index + 1);
    return;
  }
  const child = spawn(command, args, { cwd: process.cwd(), env, stdio: "inherit" });
  activeChild = child;
  child.once("error", (error) => {
    if (error.code === "ENOENT") start(index + 1);
    else {
      process.stderr.write(`EdgePilot MCP could not start: ${error.message}\n`);
      process.exit(1);
    }
  });
  child.once("exit", (code, signal) => {
    process.exit(signal ? 1 : (code ?? 1));
  });
}

start(0);
