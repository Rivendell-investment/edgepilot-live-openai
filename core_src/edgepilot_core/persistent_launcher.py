"""Versioned dashboard-only launcher copied into a product state root."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import signal
import sys
import time


def main() -> int:
    check = "--check" in sys.argv
    config_path = Path(next(value for value in sys.argv[1:] if value != "--check"))
    value = json.loads(config_path.read_text(encoding="utf-8"))
    module_path = Path(__file__).with_name("local_mcp.py")
    spec = importlib.util.spec_from_file_location("edgepilot_persistent_local_mcp", module_path)
    if spec is None or spec.loader is None: raise RuntimeError("persistent dashboard module is unavailable")
    module = importlib.util.module_from_spec(spec); sys.modules[spec.name] = module; spec.loader.exec_module(module)
    config = module.ProductConfig(**value)
    if check:
        config = module.ProductConfig(**{**value, "default_port": 0, "state_root": Path(value["state_root"]) / "health-check"})
    else:
        config = module.ProductConfig(**{**value, "state_root": Path(value["state_root"])})
    # The background service must own its listener. Reusing the current MCP's
    # listener would make the website disappear when Codex exits.
    dashboard = module.Dashboard(config, reuse_existing=False); dashboard.start()
    if dashboard.identity is None: raise RuntimeError(dashboard.error or "dashboard health check failed")
    if check:
        dashboard.close(); return 0
    stopped = False
    def stop(*_args: object) -> None:
        nonlocal stopped; stopped = True
    signal.signal(signal.SIGTERM, stop); signal.signal(signal.SIGINT, stop)
    try:
        while not stopped:
            dashboard.ensure()
            time.sleep(0.5)
    finally:
        dashboard.close()
    return 0


if __name__ == "__main__": raise SystemExit(main())
