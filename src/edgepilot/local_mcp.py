#!/usr/bin/env python3
"""EdgePilot Live local stdio MCP entry point."""

from __future__ import annotations

from pathlib import Path
import sys
from http.server import ThreadingHTTPServer

PRODUCT_ROOT = Path(__file__).resolve().parents[2]
CORE = PRODUCT_ROOT / "core_src"
if not CORE.is_dir():
    CORE = PRODUCT_ROOT.parent / "edgepilot-core" / "src"
sys.path[:0] = [str(PRODUCT_ROOT / "src"), str(CORE)]

from edgepilot import __version__  # noqa: E402
from edgepilot.marketplace import recommend  # noqa: E402
from edgepilot.local_service import ServiceDashboardClient, disable_persistent_service, enable_persistent_service  # noqa: E402
from edgepilot.paths import state_root  # noqa: E402
from edgepilot_core.local_mcp import ProductConfig, run  # noqa: E402


def _dashboard_server(host: str, port: int) -> ThreadingHTTPServer:
    from edgepilot.ui import DashboardHandler
    server = ThreadingHTTPServer((host, port), DashboardHandler)
    server.edgepilot_language = None  # type: ignore[attr-defined]
    server.edgepilot_csrf = __import__("secrets").token_urlsafe(32)  # type: ignore[attr-defined]
    return server


if __name__ == "__main__":
    dashboard = ServiceDashboardClient()
    dashboard.reconcile()
    raise SystemExit(run(ProductConfig(
        name="EdgePilot Live",
        server_name="edgepilot-live",
        version=__version__,
        state_root=state_root(),
        default_port=8787,
        ui_uri="ui://edgepilot-live/strategy-cards/v2.html",
        lifecycle_prompt=("EdgePilot Live 本地 Dashboard 已打开（仅本机可访问）。"
                          "关闭当前聊天不会中断正在运行的任务。"),
        service_id="capital.rivendell.edgepilot.live.dashboard",
        windows_task=r"\EdgePilot\Live Dashboard",
    ), recommend, dashboard_client=dashboard,
        persistent_actions=(enable_persistent_service, disable_persistent_service)))
