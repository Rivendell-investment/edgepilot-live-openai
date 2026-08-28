#!/usr/bin/env python3
"""Stop only the verified EdgePilot Live localhost service."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


PLUGIN_ROOT = Path(__file__).resolve().parents[3]
CORE = PLUGIN_ROOT / "core_src"
if not CORE.is_dir():
    CORE = PLUGIN_ROOT.parent / "edgepilot-core" / "src"
sys.path[:0] = [str(PLUGIN_ROOT / "src"), str(CORE)]

from edgepilot.service.local_service import stop_verified_service  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stop the verified EdgePilot Live local service")
    parser.add_argument("--force", action="store_true", help="Force-stop the same verified PID after timeout")
    args = parser.parse_args(argv)
    print(json.dumps(stop_verified_service(force=args.force), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
