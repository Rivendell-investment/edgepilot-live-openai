"""Explicit macOS/Windows persistent Dashboard registration."""

from __future__ import annotations

import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any


STABLE_LAUNCHER = """#!/usr/bin/env python3
import json, os, pathlib, sys
root=pathlib.Path(__file__).resolve().parent
active=json.loads((root/'active.json').read_text(encoding='utf-8'))
launcher=(root/active['launcher']).resolve(); config=(root/active['config']).resolve()
if root not in launcher.parents or root not in config.parents: raise SystemExit('unsafe active dashboard path')
os.execv(sys.executable,[sys.executable,str(launcher),str(config)])
"""


def _atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(data); output.flush(); os.fsync(output.fileno())
        os.replace(temporary, path)
    finally: Path(temporary).unlink(missing_ok=True)


def _run(command: list[str]) -> None:
    subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _register(root: Path, service_id: str, windows_task: str, *, restart: bool = True) -> None:
    launcher = root / "launcher.py"
    if sys.platform == "darwin":
        agents = Path.home() / "Library" / "LaunchAgents"; agents.mkdir(parents=True, exist_ok=True)
        plist = agents / f"{service_id}.plist"
        payload = {"Label": service_id, "ProgramArguments": [sys.executable, str(launcher)], "RunAtLoad": True,
                   "KeepAlive": False, "StandardOutPath": str(root / "service.stdout.log"), "StandardErrorPath": str(root / "service.stderr.log")}
        _atomic(plist, plistlib.dumps(payload))
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(plist)], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        _run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)])
        if restart:
            _run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{service_id}"])
    elif os.name == "nt":
        _run(["schtasks", "/Create", "/TN", windows_task, "/TR", f'"{sys.executable}" "{launcher}"', "/SC", "ONLOGON", "/F"])
        if restart:
            _run(["schtasks", "/Run", "/TN", windows_task])
    else:
        raise RuntimeError("长期运行当前只支持 macOS 和 Windows")


def register_launcher(root: Path, service_id: str, windows_task: str, *, restart: bool = True) -> None:
    """Register a product-owned stable launcher using the shared OS adapter."""
    _register(root, service_id, windows_task, restart=restart)


def _unregister(root: Path, service_id: str, windows_task: str) -> None:
    if sys.platform == "darwin":
        plist = Path.home() / "Library" / "LaunchAgents" / f"{service_id}.plist"
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(plist)], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        plist.unlink(missing_ok=True)
    elif os.name == "nt":
        subprocess.run(["schtasks", "/End", "/TN", windows_task], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["schtasks", "/Delete", "/TN", windows_task, "/F"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        raise RuntimeError("长期运行当前只支持 macOS 和 Windows")


def unregister_launcher(root: Path, service_id: str, windows_task: str) -> None:
    """Unregister a product-owned launcher without deleting product state."""
    _unregister(root, service_id, windows_task)


def _wait_for_service(config: Any, *, previous_pid: object, timeout: float = 10.0) -> dict[str, Any]:
    from edgepilot_core.local_mcp import _trusted_dashboard
    record_path = config.state_root / "local-dashboard.json"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            record = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            record = None
        trusted = _trusted_dashboard(record, config)
        if trusted is not None and trusted.get("pid") not in {os.getpid(), previous_pid}:
            return trusted
        time.sleep(0.1)
    raise RuntimeError("后台 Dashboard 未能在 10 秒内通过版本、PID 和实例标识校验")


def install(config: Any, *, service_id: str, windows_task: str) -> dict[str, Any]:
    root = config.state_root / "background-dashboard"; version = root / "versions" / config.version
    version.mkdir(parents=True, exist_ok=True)
    source = Path(__file__).with_name("local_mcp.py"); launcher_source = Path(__file__).with_name("persistent_launcher.py")
    shutil.copyfile(source, version / "local_mcp.py"); shutil.copyfile(launcher_source, version / "persistent_launcher.py")
    config_value = {key: value for key, value in vars(config).items()}; config_value["state_root"] = str(config.state_root)
    _atomic(version / "config.json", (json.dumps(config_value, ensure_ascii=False, sort_keys=True) + "\n").encode())
    _run([sys.executable, str(version / "persistent_launcher.py"), str(version / "config.json"), "--check"])
    root.mkdir(parents=True, exist_ok=True); _atomic(root / "launcher.py", STABLE_LAUNCHER.encode())
    active = {"launcher": f"versions/{config.version}/persistent_launcher.py", "config": f"versions/{config.version}/config.json"}
    old = (root / "active.json").read_bytes() if (root / "active.json").is_file() else None
    try:
        previous_record = json.loads((config.state_root / "local-dashboard.json").read_text(encoding="utf-8"))
        previous_pid = previous_record.get("pid") if isinstance(previous_record, dict) else None
        if type(previous_pid) is not int:
            previous_pid = None
    except (OSError, ValueError, json.JSONDecodeError):
        previous_pid = None
    _atomic(root / "active.json", (json.dumps(active, sort_keys=True) + "\n").encode())
    try:
        _register(root, service_id, windows_task)
        identity = _wait_for_service(config, previous_pid=previous_pid)
    except BaseException:
        if old is None:
            (root / "active.json").unlink(missing_ok=True)
            try:
                _unregister(root, service_id, windows_task)
            except BaseException:
                pass
        else:
            _atomic(root / "active.json", old)
            try:
                _register(root, service_id, windows_task)
            except BaseException:
                pass
        raise
    for candidate in (root / "versions").iterdir():
        if candidate.is_dir() and candidate.name != config.version: shutil.rmtree(candidate)
    return {"enabled": True, "service_id": service_id, "windows_task": windows_task,
            "version": config.version, "url": identity["url"], "pid": identity["pid"]}


def uninstall(config: Any, *, service_id: str, windows_task: str) -> dict[str, Any]:
    root = config.state_root / "background-dashboard"
    _unregister(root, service_id, windows_task)
    if root.exists(): shutil.rmtree(root)
    return {"enabled": False, "service_id": service_id, "windows_task": windows_task}
