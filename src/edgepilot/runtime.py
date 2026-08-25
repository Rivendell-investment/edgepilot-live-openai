"""Lightweight access to the delayed Live runtime installer and active Python."""

from __future__ import annotations

import importlib.util
import json
from contextlib import redirect_stdout
import os
from pathlib import Path
import sys
from typing import Any, Callable
import urllib.request

def plugin_root() -> Path:
    return Path(__file__).resolve().parents[2]


def runtime_venv() -> Path:
    override = os.environ.get("EDGEPILOT_VENV")
    if override:
        return Path(override).expanduser().resolve()
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "EdgePilot" / ".venv"
    return Path.home() / ".edgepilot" / ".venv"


def _installer():
    path = plugin_root() / "skills" / "edgepilot" / "scripts" / "install_runtime.py"
    spec = importlib.util.spec_from_file_location("edgepilot_delayed_runtime_installer", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("EdgePilot runtime installer is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def runtime_status() -> dict[str, Any]:
    venv = runtime_venv()
    root = venv.parent
    state_path = root / "runtime.json"
    python = venv / ("Scripts/python.exe" if __import__("os").name == "nt" else "bin/python")
    if not state_path.is_file():
        return {"installed": False, "home": str(root), "install_path": str(venv)}
    try:
        value = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {"installed": False, "home": str(root), "install_path": str(venv), "incomplete": True}
    installed = value.get("schema_version") == 1 and value.get("active_release") == ".venv" and python.is_file()
    return {**value, "installed": installed, "home": str(root), "install_path": str(venv), "python_executable": str(python)}


def active_runtime_python() -> Path:
    status = runtime_status()
    if not status.get("installed"):
        raise ValueError("RUNTIME_NOT_INSTALLED: install the EdgePilot Live runtime before continuing")
    # Do not resolve the POSIX venv symlink to the base interpreter: subprocesses
    # must enter the installed venv and load its locked packages.
    return Path(str(status["python_executable"]))


def runtime_install_info() -> dict[str, Any]:
    module = _installer()
    try:
        version = module.read_pinned_nautilus_version(plugin_root())
        manifest_url = f"{module.DEFAULT_WHEEL_BASE_URL.rstrip('/')}/manifest.json"
        with urllib.request.urlopen(module.http_request(manifest_url), timeout=3) as response:
            manifest = json.loads(response.read(1024 * 1024))
        selection = module.select_from_manifest(manifest, version, manifest_url)
    except SystemExit as exc:
        raise ValueError(str(exc) or "EdgePilot runtime is unavailable for this platform") from exc
    return {
        "product_name": "EdgePilot Live",
        "download_size": selection.bytes,
        "version": version,
        "install_path": str(runtime_venv()),
    }


def install_runtime(progress: Callable[[str, str, int | None, int | None], None]) -> None:
    module = _installer()
    module.PROGRESS_CALLBACK = progress
    try:
        with redirect_stdout(sys.stderr):
            result = module.main([str(plugin_root())])
        if result != 0:
            raise RuntimeError(f"EdgePilot runtime installer exited with status {result}")
    finally:
        module.PROGRESS_CALLBACK = None
