"""Native-runtime worker for the lightweight Live Dashboard."""

from __future__ import annotations

import json
import sys
from typing import Any


def _dispatch(operation: str, payload: dict[str, Any]) -> Any:
    from edgepilot.dashboard import native as dashboard_native

    if operation == "strategy_config":
        return dashboard_native.strategy_config(payload)
    if operation == "strategy_detail":
        return dashboard_native.strategy_detail(payload)
    if operation == "prepare_backtest":
        return dashboard_native.prepare_backtest(payload)
    if operation == "credentials":
        return dashboard_native.credential_records()
    if operation == "save_credentials":
        return dashboard_native.save_credentials(payload)
    if operation == "create_strategy_config":
        return dashboard_native.create_strategy_config(
            str(payload.get("strategy", "")),
            str(payload.get("name", "")),
            payload.get("config"),
        )
    if operation == "update_strategy_config":
        return dashboard_native.update_strategy_config(
            str(payload.get("strategy", "")),
            str(payload.get("name", "")),
            payload.get("config"),
        )
    raise ValueError("unknown Dashboard runtime operation")


def main() -> int:
    try:
        request = json.loads(sys.stdin.read())
        if not isinstance(request, dict) or set(request) != {"operation", "payload"}:
            raise ValueError("invalid Dashboard runtime request")
        operation, payload = request["operation"], request["payload"]
        if not isinstance(operation, str) or not isinstance(payload, dict):
            raise ValueError("invalid Dashboard runtime request")
        response = {"ok": True, "result": _dispatch(operation, payload)}
    except Exception as error:
        response = {
            "ok": False,
            "error": {
                "type": type(error).__name__,
                "message": str(error) or type(error).__name__,
            },
        }
    sys.stdout.write(json.dumps(response, default=str, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
