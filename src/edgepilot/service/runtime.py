"""Lightweight access to the delayed Live runtime installer and active Python."""

from __future__ import annotations

import importlib.util
import hashlib
import json
from contextlib import redirect_stdout
import os
from pathlib import Path
import sys
from typing import Any, Callable
import urllib.request

from edgepilot import __version__
from edgepilot.service.build_identity import plugin_content_digest, runtime_contract_digest

def plugin_root() -> Path:
    return Path(__file__).resolve().parents[3]


def runtime_venv() -> Path:
    override = os.environ.get("EDGEPILOT_VENV")
    if override:
        return Path(override).expanduser().resolve()
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
    try:
        expected_digest = plugin_content_digest(plugin_root())
    except (OSError, ValueError):
        expected_digest = None
    try:
        runtime_python = str(value["runtime_python"])
        module = _installer()
        contract_digest = runtime_contract_digest(plugin_root(), runtime_python, module.DEFAULT_WHEEL_BASE_URL)
        expected_native_fingerprint = hashlib.sha256(
            f"{contract_digest}\0{value['nautilus_version']}\0{value['wheel_filename']}\0"
            f"{value['wheel_sha256']}\0{runtime_python}".encode()
        ).hexdigest()
    except (KeyError, OSError, ValueError, TypeError):
        contract_digest = None
        expected_native_fingerprint = None
    native_installed = (
        value.get("schema_version") == 3
        and value.get("active_release") == ".venv"
        and contract_digest is not None
        and value.get("runtime_contract_digest") == contract_digest
        and value.get("native_runtime_fingerprint") == expected_native_fingerprint
        and python.is_file()
    )
    plugin_current = (
        value.get("plugin_version") == __version__
        and expected_digest is not None
        and value.get("plugin_content_digest") == expected_digest
    )
    installed = native_installed and plugin_current
    reason = None
    if python.is_file() and not native_installed:
        reason = "NATIVE_RUNTIME_MISMATCH: Python or native dependencies do not match this EdgePilot runtime contract"
    elif native_installed and not plugin_current:
        reason = "PLUGIN_UPDATE_REQUIRED: native runtime is reusable but the EdgePilot package must be updated"
    return {
        **value,
        "installed": installed,
        "home": str(root),
        "install_path": str(venv),
        "python_executable": str(python),
        "expected_plugin_version": __version__,
        "expected_plugin_content_digest": expected_digest,
        "expected_runtime_contract_digest": contract_digest,
        "native_installed": native_installed,
        "plugin_update_required": native_installed and not plugin_current,
        "incomplete": python.is_file() and not installed,
        "mismatch_reason": reason,
        "release_id": value.get("native_runtime_fingerprint"),
    }


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
