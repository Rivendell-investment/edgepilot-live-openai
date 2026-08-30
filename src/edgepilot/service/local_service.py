"""Single-instance lifecycle for the EdgePilot Live localhost service.

The Codex MCP, the CLI, and the optional OS launcher are clients of this
process.  The service owns the Dashboard listener and Dashboard jobs; a chat
lease ending never directly terminates either one.
"""

from __future__ import annotations

import argparse
import errno
import http.client
import json
import os
from pathlib import Path
import re
import secrets
import signal
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from threading import Event, Lock, Thread
from typing import Any

from edgepilot import __version__
from edgepilot.platform.logging import configure_logging
from edgepilot.service.build_identity import (
    plugin_content_digest as _plugin_content_digest,
    verified_plugin_content_digest as _verified_plugin_content_digest,
)
from edgepilot.platform.paths import state_root


SERVICE_PROTOCOL = 1
SERVICE_SCHEMA = 2
SERVICE_RECORD = "local-dashboard.json"
SERVICE_LOCK = "local-dashboard.lock"
START_LOCK = "local-dashboard-start.lock"
PERSISTENT_MARKER = "background-dashboard/enabled.json"
PENDING_GENERATION = "local-dashboard.pending.json"
SERVICE_ID = "capital.rivendell.edgepilot.live.dashboard"
WINDOWS_TASK = r"\EdgePilot\Live Dashboard"
BROWSER_HANDOFF_SECONDS = 300.0
BROWSER_LEASE_SECONDS = 180.0
BROWSER_HANDOFF_LEASE_ID = "browser-handoff"
BROWSER_LEASE_ID = "browser"
SERVICE_FIELDS = {
    "schema_version",
    "pid",
    "instance_nonce",
    "host",
    "port",
    "url",
    "product_version",
    "build_id",
    "service_protocol",
    "python_executable",
    "python_version",
    "started_at",
}

LOGGER = configure_logging()


class ServiceConflict(RuntimeError):
    """Another verified Live service owns the state root."""


FIXED_HOST = "127.0.0.1"
FIXED_PORT = 8787


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(value, output, sort_keys=True, separators=(",", ":"))
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        if os.name != "nt":
            os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _read_json(path: Path) -> Any:
    if not path.is_file() or path.stat().st_size > 256 * 1024:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def pid_exists(pid: object) -> bool:
    if type(pid) is not int or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def plugin_content_digest(root: Path | None = None) -> str:
    """Return a deterministic identity for executable Live product bytes."""
    if root is None:
        from edgepilot.service.runtime import plugin_root

        root = plugin_root()
    return _plugin_content_digest(root)


def verified_plugin_build_id(root: Path | None = None) -> str:
    """Reject a packaged cache tree whose executable bytes were overlaid."""
    if root is None:
        from edgepilot.service.runtime import plugin_root

        root = plugin_root()
    return _verified_plugin_content_digest(root, expected_version=__version__)


def _version_tuple(value: object) -> tuple[int, int, int] | None:
    if not isinstance(value, str):
        return None
    parts = value.split(".")
    if len(parts) != 3 or any(not part.isdigit() for part in parts):
        return None
    return tuple(int(part) for part in parts)  # type: ignore[return-value]


def host_version_key(value: object) -> tuple[int, int, int, int, int] | None:
    """Order official versions and timestamped Codex development builds."""
    if not isinstance(value, str):
        return None
    base, separator, cachebuster = value.partition("+codex.")
    version = _version_tuple(base)
    if version is None:
        return None
    if not separator:
        return (*version, 0, 0)
    if not re.fullmatch(r"[0-9]{14}", cachebuster):
        return None
    return (*version, 1, int(cachebuster))


def plugin_host_version(root: Path | None = None) -> str:
    """Return the Codex cache identity while keeping Python SemVer stable."""
    if root is None:
        from edgepilot.service.runtime import plugin_root

        root = plugin_root()
    for name in ("BUILD.json", ".complete.json"):
        build = _read_json(root / name)
        if not isinstance(build, dict) or build.get("product_version") != __version__:
            continue
        value = build.get("host_plugin_version")
        if host_version_key(value) is not None:
            return str(value)
    return __version__


def service_identity(host: str, port: int, nonce: str, build_id: str) -> dict[str, Any]:
    url = f"http://{host}:{port}"
    return {
        "schema_version": SERVICE_SCHEMA,
        "pid": os.getpid(),
        "instance_nonce": nonce,
        "host": host,
        "port": port,
        "url": url,
        "product_version": __version__,
        "build_id": build_id,
        "service_protocol": SERVICE_PROTOCOL,
        "python_executable": str(Path(sys.executable).absolute()),
        "python_version": ".".join(map(str, sys.version_info[:3])),
        "started_at": datetime.now(timezone.utc).isoformat(),
    }


def service_request(
    record: dict[str, Any],
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    timeout: float = 1.5,
) -> dict[str, Any] | None:
    host, port, nonce = record.get("host"), record.get("port"), record.get("instance_nonce")
    if host != "127.0.0.1" or type(port) is not int or not isinstance(nonce, str):
        return None
    connection: http.client.HTTPConnection | None = None
    try:
        connection = http.client.HTTPConnection(host, port, timeout=timeout)
        headers = {"Host": f"{host}:{port}", "X-EdgePilot-Instance": nonce}
        payload = None
        if body is not None:
            payload = json.dumps(body, separators=(",", ":")).encode()
            headers["Content-Type"] = "application/json"
        connection.request(method, path, body=payload, headers=headers)
        response = connection.getresponse()
        raw = response.read(256 * 1024)
        value = json.loads(raw) if raw else {}
        return value if response.status in {200, 201, 202} and isinstance(value, dict) else None
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    finally:
        if connection is not None:
            connection.close()


def health_matches(record: dict[str, Any]) -> bool:
    value = service_request(record, "GET", "/api/health")
    return isinstance(value, dict) and all(value.get(key) == record.get(key) for key in SERVICE_FIELDS)


def trusted_service(record: object, *, expected_build_id: str | None = None) -> dict[str, Any] | None:
    if not isinstance(record, dict) or set(record) != SERVICE_FIELDS:
        return None
    if record.get("schema_version") != SERVICE_SCHEMA or record.get("service_protocol") != SERVICE_PROTOCOL:
        return None
    if expected_build_id is not None and record.get("build_id") != expected_build_id:
        return None
    if record.get("host") != "127.0.0.1" or type(record.get("port")) is not int:
        return None
    if not isinstance(record.get("instance_nonce"), str) or len(record["instance_nonce"]) < 20:
        return None
    if not pid_exists(record.get("pid")) or not health_matches(record):
        return None
    return record


def read_service_record(root: Path | None = None) -> dict[str, Any] | None:
    value = _read_json((root or state_root()) / SERVICE_RECORD)
    return value if isinstance(value, dict) else None


def acquire_service_lock(root: Path, nonce: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    lock = root / SERVICE_LOCK
    try:
        lock.mkdir(mode=0o700)
    except FileExistsError:
        owner = _read_json(lock / "owner.json")
        owner_pid = owner.get("pid") if isinstance(owner, dict) else None
        record = read_service_record(root)
        if pid_exists(owner_pid) or (record is not None and trusted_service(record) is not None):
            raise ServiceConflict("another EdgePilot Live service is already running")
        if not isinstance(owner_pid, int) and time.time() - lock.stat().st_mtime < 30:
            raise ServiceConflict("another EdgePilot Live service is starting")
        shutil.rmtree(lock)
        (root / SERVICE_RECORD).unlink(missing_ok=True)
        try:
            lock.mkdir(mode=0o700)
        except FileExistsError as error:
            raise ServiceConflict("another EdgePilot Live service is starting") from error
    _atomic_json(lock / "owner.json", {
        "pid": os.getpid(), "instance_nonce": nonce, "started_at": int(time.time()),
    })
    return lock


def release_service_lock(root: Path, nonce: str) -> None:
    record = read_service_record(root)
    if isinstance(record, dict) and secrets.compare_digest(str(record.get("instance_nonce", "")), nonce):
        (root / SERVICE_RECORD).unlink(missing_ok=True)
    lock = root / SERVICE_LOCK
    owner = _read_json(lock / "owner.json")
    if isinstance(owner, dict) and secrets.compare_digest(str(owner.get("instance_nonce", "")), nonce):
        shutil.rmtree(lock, ignore_errors=True)


class ServiceState:
    def __init__(self, root: Path, identity: dict[str, Any], *, idle_seconds: float = 30.0) -> None:
        self.root = root
        self.identity = identity
        self.idle_seconds = idle_seconds
        self.ready_monotonic = time.monotonic()
        self.idle_since: float | None = None
        self.browser_connected = False
        self.leases: dict[str, tuple[str, float]] = {}
        self.lock = Lock()

    def acquire_lease(self, client_type: str = "chat", *, seconds: float = 30.0, lease_id: str | None = None) -> str:
        lease_id = lease_id or secrets.token_urlsafe(24)
        with self.lock:
            now = time.monotonic()
            self.leases[lease_id] = (client_type, now + max(0.0, seconds))
            self.idle_since = None
        return lease_id

    def renew_lease(self, lease_id: str) -> bool:
        with self.lock:
            if lease_id not in self.leases:
                return False
            client_type, _ = self.leases[lease_id]
            now = time.monotonic()
            self.leases[lease_id] = (client_type, now + 30)
            self.idle_since = None
        return True

    def release_lease(self, lease_id: str) -> bool:
        with self.lock:
            released = self.leases.pop(lease_id, None) is not None
            if released and not self.leases:
                self.idle_since = time.monotonic()
        return released

    def begin_browser_handoff(self, seconds: float = BROWSER_HANDOFF_SECONDS) -> int:
        self.acquire_lease("browser_handoff", seconds=seconds, lease_id=BROWSER_HANDOFF_LEASE_ID)
        return max(0, round(seconds))

    def touch_browser(self) -> float | None:
        with self.lock:
            now = time.monotonic()
            self.idle_since = None
            self.leases.pop(BROWSER_HANDOFF_LEASE_ID, None)
            self.leases[BROWSER_LEASE_ID] = ("browser", now + BROWSER_LEASE_SECONDS)
            if self.browser_connected:
                return None
            self.browser_connected = True
            return max(0.0, now - self.ready_monotonic)

    def has_leases(self) -> bool:
        now = time.monotonic()
        with self.lock:
            self.leases = {
                key: (client_type, expiry)
                for key, (client_type, expiry) in self.leases.items()
                if expiry > now
            }
            return bool(self.leases)

    def should_stop(self, *, external_keepalive: bool, now: float | None = None) -> bool:
        """Return true only after every keepalive has been absent for the idle grace period."""
        now = time.monotonic() if now is None else now
        with self.lock:
            self.leases = {
                key: (client_type, expiry)
                for key, (client_type, expiry) in self.leases.items()
                if expiry > now
            }
            if external_keepalive or self.leases:
                self.idle_since = None
                return False
            if self.idle_since is None:
                self.idle_since = now
                return False
            return now - self.idle_since >= self.idle_seconds


def _process_environment() -> dict[str, str]:
    from edgepilot.service.runtime import plugin_root

    root = plugin_root()
    core = root / "core_src"
    if not core.is_dir():
        core = root.parent / "edgepilot-core" / "src"
    environment = dict(os.environ)
    environment.pop("PYTHONHOME", None)
    environment["PYTHONPATH"] = os.pathsep.join((str(root / "src"), str(core)))
    return environment


def _copy_generation(source: Path, destination: Path) -> None:
    from edgepilot.service.build_identity import canonical_pyproject

    (destination / "src").mkdir(parents=True)
    shutil.copytree(
        source / "src" / "edgepilot",
        destination / "src" / "edgepilot",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    bundled = source / "core_src" / "edgepilot_core"
    repository = source.parent / "edgepilot-core" / "src" / "edgepilot_core"
    core = bundled if bundled.is_dir() else repository
    shutil.copytree(
        core,
        destination / "core_src" / "edgepilot_core",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    (destination / "pyproject.toml").write_bytes(canonical_pyproject((source / "pyproject.toml").read_bytes()))
    if (source / "BUILD.json").is_file():
        shutil.copy2(source / "BUILD.json", destination / "BUILD.json")
    installer = source / "skills" / "edgepilot" / "scripts"
    if installer.is_dir():
        shutil.copytree(
            installer,
            destination / "skills" / "edgepilot" / "scripts",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )


def stage_generation(root: Path | None = None) -> tuple[str, Path]:
    """Copy the current plugin into an immutable user-owned generation."""
    from edgepilot.service.runtime import plugin_root

    source = plugin_root()
    build_id = verified_plugin_build_id(source)
    service_root = (root or state_root()) / "background-dashboard"
    destination = service_root / "installations" / build_id
    complete = destination / ".complete.json"
    if complete.is_file() and plugin_content_digest(destination) == build_id:
        return build_id, destination
    if destination.exists():
        shutil.rmtree(destination)
    candidate = destination.with_name(f".{build_id}.candidate-{os.getpid()}")
    if candidate.exists():
        shutil.rmtree(candidate)
    candidate.parent.mkdir(parents=True, exist_ok=True)
    try:
        _copy_generation(source, candidate)
        if plugin_content_digest(candidate) != build_id:
            raise RuntimeError("staged EdgePilot generation digest does not match its source")
        _atomic_json(candidate / ".complete.json", {
            "build_id": build_id,
            "product_version": __version__,
            "host_plugin_version": plugin_host_version(source),
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
        try:
            candidate.rename(destination)
        except FileExistsError:
            shutil.rmtree(candidate)
    finally:
        if candidate.exists():
            shutil.rmtree(candidate)
    return build_id, destination


_STABLE_LAUNCHER = """#!/usr/bin/env python3
import json, os, pathlib, runpy, sys
root=pathlib.Path(__file__).resolve().parent
active=json.loads((root/'active.json').read_text(encoding='utf-8'))
generation=(root/'installations'/active['build_id']).resolve()
if generation.parent != (root/'installations').resolve(): raise SystemExit('unsafe EdgePilot generation')
sys.path[:0]=[str(generation/'src'),str(generation/'core_src')]
os.environ['EDGEPILOT_ACTIVE_BUILD_ID']=active['build_id']
runpy.run_module('edgepilot.local_service',run_name='__main__')
"""


def activate_persistent_generation(root: Path | None = None) -> dict[str, Any]:
    service_root = (root or state_root()) / "background-dashboard"
    build_id, _ = stage_generation(root)
    _atomic_json(service_root / "active.json", {
        "schema_version": 1,
        "build_id": build_id,
        "product_version": __version__,
        "python_executable": str(Path(sys.executable).absolute()),
        "activated_at": datetime.now(timezone.utc).isoformat(),
    })
    launcher = service_root / "launcher.py"
    temporary = launcher.with_name(f".{launcher.name}.{os.getpid()}.tmp")
    temporary.write_text(_STABLE_LAUNCHER, encoding="utf-8")
    if os.name != "nt":
        temporary.chmod(0o700)
    temporary.replace(launcher)
    return {"build_id": build_id, "launcher": str(launcher)}


def enable_persistent_service() -> dict[str, Any]:
    from edgepilot_core.persistent_service import register_launcher

    root = state_root()
    marker_path = root / PERSISTENT_MARKER
    previous_marker = marker_path.read_bytes() if marker_path.is_file() else None
    already_enabled = previous_marker is not None
    activation = activate_persistent_generation(root)
    _atomic_json(marker_path, {
        "schema_version": 1,
        "enabled_at": datetime.now(timezone.utc).isoformat(),
        "build_id": activation["build_id"],
    })
    try:
        identity = ensure_service()
        if not already_enabled:
            register_launcher(root / "background-dashboard", SERVICE_ID, WINDOWS_TASK, restart=False)
    except BaseException:
        if previous_marker is None:
            marker_path.unlink(missing_ok=True)
        else:
            temporary = marker_path.with_name(f".{marker_path.name}.{os.getpid()}.rollback")
            temporary.write_bytes(previous_marker)
            temporary.replace(marker_path)
        raise
    return {
        "enabled": True,
        "service_id": SERVICE_ID,
        "windows_task": WINDOWS_TASK,
        "build_id": activation["build_id"],
        "url": identity["url"],
        "pid": identity["pid"],
    }


def disable_persistent_service() -> dict[str, Any]:
    from edgepilot_core.persistent_service import unregister_launcher

    root = state_root()
    record = trusted_service(read_service_record(root))
    status = service_request(record, "GET", "/api/process/status") if record is not None else None
    if isinstance(status, dict) and status.get("active_work"):
        raise RuntimeError("ACTIVE_WORK: stop active runs and jobs before disabling the background service")
    unregister_launcher(root / "background-dashboard", SERVICE_ID, WINDOWS_TASK)
    (root / PERSISTENT_MARKER).unlink(missing_ok=True)
    return {"enabled": False, "service_id": SERVICE_ID, "windows_task": WINDOWS_TASK}


def _wait_for_exit(pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + max(0.0, timeout)
    while pid_exists(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    return not pid_exists(pid)


def stop_verified_service(*, force: bool = False, timeout: float = 5.0) -> dict[str, Any]:
    """Stop only the service authenticated by the owner-only service record."""
    from edgepilot.execution.run_state import process_start_token

    root = state_root()
    record = trusted_service(read_service_record(root))
    if record is None:
        return {"stopped": False, "reason": "not_running"}
    status = service_request(record, "GET", "/api/process/status")
    if not isinstance(status, dict) or type(status.get("active_work")) is not bool:
        raise RuntimeError("EDGEPILOT_SERVICE_STATUS_UNAVAILABLE: refusing to stop a service with unknown work state")
    if status.get("active_work"):
        raise RuntimeError("ACTIVE_WORK: stop active runs and jobs before replacing EdgePilot Live")
    pid = int(record["pid"])
    start_token = process_start_token(pid)
    response = service_request(record, "POST", "/api/process/stop", body={
        "instance_nonce": record["instance_nonce"],
        "reason": "maintenance",
    })
    if isinstance(response, dict) and response.get("stopping") is True and _wait_for_exit(pid, timeout):
        return {"stopped": True, "forced": False, "pid": pid}
    if not pid_exists(pid):
        return {"stopped": True, "forced": False, "pid": pid}
    if not force:
        raise RuntimeError("EDGEPILOT_SERVICE_STOP_TIMEOUT: the verified EdgePilot service did not exit")
    current = read_service_record(root)
    if not isinstance(current, dict) or current.get("pid") != pid \
            or current.get("instance_nonce") != record.get("instance_nonce"):
        raise RuntimeError("EDGEPILOT_SERVICE_IDENTITY_CHANGED: refusing to terminate an unverified process")
    if start_token is None or process_start_token(pid) != start_token:
        raise RuntimeError("EDGEPILOT_SERVICE_IDENTITY_CHANGED: refusing to terminate a reused PID")
    os.kill(pid, signal.SIGTERM)
    if not _wait_for_exit(pid, min(2.0, timeout)):
        if process_start_token(pid) != start_token:
            raise RuntimeError("EDGEPILOT_SERVICE_IDENTITY_CHANGED: refusing to terminate a reused PID")
        kill_signal = getattr(signal, "SIGKILL", signal.SIGTERM)
        os.kill(pid, kill_signal)
        if not _wait_for_exit(pid, min(2.0, timeout)):
            raise RuntimeError("EDGEPILOT_SERVICE_FORCE_STOP_FAILED: the verified EdgePilot service is still running")
    return {"stopped": True, "forced": True, "pid": pid}


def _spawn_service(root: Path, port: int) -> None:
    logs = root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    output = (logs / "local-service.log").open("ab", buffering=0)
    kwargs: dict[str, Any] = {
        "cwd": str(root),
        "env": _process_environment(),
        "stdin": subprocess.DEVNULL,
        "stdout": output,
        "stderr": subprocess.STDOUT,
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen(
            [sys.executable, "-m", "edgepilot.local_service", "--port", str(port)],
            **kwargs,
        )
    finally:
        output.close()


def _acquire_start_lock(root: Path) -> Path | None:
    lock = root / START_LOCK
    try:
        lock.mkdir(parents=True, mode=0o700)
    except FileExistsError:
        owner = _read_json(lock / "owner.json")
        if isinstance(owner, dict) and pid_exists(owner.get("pid")):
            return None
        if time.time() - lock.stat().st_mtime < 15:
            return None
        shutil.rmtree(lock, ignore_errors=True)
        try:
            lock.mkdir(mode=0o700)
        except FileExistsError:
            return None
    _atomic_json(lock / "owner.json", {"pid": os.getpid(), "started_at": int(time.time())})
    return lock


def _replacement_payload(old: dict[str, Any], build_id: str) -> dict[str, Any]:
    return {
        "instance_nonce": old["instance_nonce"],
        "replacement_product_version": __version__,
        "replacement_host_version": plugin_host_version(),
        "replacement_build_id": build_id,
    }


def _running_host_version(old: dict[str, Any], status: dict[str, Any]) -> object:
    value = status.get("host_plugin_version")
    return value if host_version_key(value) is not None else old.get("product_version")


def _candidate_is_newer(old: dict[str, Any], status: dict[str, Any]) -> bool:
    running = host_version_key(_running_host_version(old, status))
    candidate = host_version_key(plugin_host_version())
    return running is not None and candidate is not None and candidate > running


def _pending_requests_upgrade(pending: object, current_host_version: str, build_id: str) -> bool:
    if not isinstance(pending, dict) or pending.get("build_id") == build_id:
        return False
    pending_key = host_version_key(pending.get("host_plugin_version"))
    current_key = host_version_key(current_host_version)
    return pending_key is not None and current_key is not None and pending_key > current_key


def _request_service_replacement(
    old: dict[str, Any],
    status: dict[str, Any],
    build_id: str,
) -> bool:
    response = service_request(
        old,
        "POST",
        "/api/process/stop",
        body=_replacement_payload(old, build_id),
    )
    if isinstance(response, dict) and response.get("stopping") is True:
        return True
    # Services predating the replacement contract only accept the nonce. The
    # fallback is one-way: a newer service advertises replacement_protocol and
    # therefore can never be stopped by a legacy MCP request.
    if status.get("replacement_protocol") is not None:
        return False
    old_version = _version_tuple(old.get("product_version"))
    current_version = _version_tuple(__version__)
    if old_version is None or current_version is None or old_version > current_version:
        return False
    legacy = service_request(
        old,
        "POST",
        "/api/process/stop",
        body={"instance_nonce": old["instance_nonce"]},
    )
    return isinstance(legacy, dict) and legacy.get("stopping") is True


def _prepare_service_replacement(
    root: Path,
    old: dict[str, Any],
    build_id: str,
    *,
    timeout: float,
) -> dict[str, Any] | None:
    status = service_request(old, "GET", "/api/process/status") or {}
    if not _candidate_is_newer(old, status):
        raise RuntimeError("EDGEPILOT_SERVICE_REPLACEMENT_REJECTED: an older plugin cannot replace this service")
    _atomic_json(root / PENDING_GENERATION, {
        "build_id": build_id,
        "product_version": __version__,
        "host_plugin_version": plugin_host_version(),
        "requested_at": int(time.time()),
    })
    if status.get("active_work"):
        return {
            **old,
            "upgrade_pending": True,
            "requested_build_id": build_id,
            "upgrade_blocked_by": "active_work",
        }
    stopping = _request_service_replacement(old, status, build_id)
    if not stopping and pid_exists(old.get("pid")):
        raise RuntimeError("EDGEPILOT_SERVICE_REPLACEMENT_REJECTED: the running service refused a version handoff")
    deadline = time.monotonic() + min(timeout, 5)
    while pid_exists(old.get("pid")) and time.monotonic() < deadline:
        time.sleep(0.05)
    if pid_exists(old.get("pid")):
        raise RuntimeError("EDGEPILOT_SERVICE_REPLACEMENT_TIMEOUT: the previous service did not exit")
    return None


def _sync_persistent_generation(root: Path, build_id: str) -> None:
    if not (root / PERSISTENT_MARKER).is_file():
        return
    active = _read_json(root / "background-dashboard/active.json")
    if isinstance(active, dict) and active.get("build_id") == build_id:
        return
    activation = activate_persistent_generation(root)
    if activation.get("build_id") != build_id:
        raise RuntimeError("EDGEPILOT_PERSISTENT_GENERATION_MISMATCH: background activation used another build")


def reconcile_existing_service(*, timeout: float = 5.0) -> dict[str, Any] | None:
    """Retire an idle older generation without starting a Dashboard."""
    root = state_root()
    root.mkdir(parents=True, exist_ok=True)
    build_id = verified_plugin_build_id()
    _sync_persistent_generation(root, build_id)
    current = read_service_record(root)
    exact = trusted_service(current, expected_build_id=build_id)
    if exact is not None:
        return exact
    old = trusted_service(current)
    if old is None:
        return None
    return _prepare_service_replacement(root, old, build_id, timeout=timeout)


def ensure_service(*, port: int = FIXED_PORT, timeout: float = 10.0) -> dict[str, Any]:
    started = time.monotonic()
    root = state_root()
    root.mkdir(parents=True, exist_ok=True)
    build_id = verified_plugin_build_id()
    _sync_persistent_generation(root, build_id)
    current = read_service_record(root)
    exact = trusted_service(current, expected_build_id=build_id)
    if exact is not None:
        LOGGER.info("local service reused", extra={
            "event": "local_service.ensure.completed", "result": "reused",
            "duration_ms": round((time.monotonic() - started) * 1000),
        })
        return exact
    old = trusted_service(current)
    if old is not None:
        deferred = _prepare_service_replacement(root, old, build_id, timeout=timeout)
        if deferred is not None:
            LOGGER.info("local service upgrade deferred", extra={
                "event": "local_service.ensure.completed", "result": "upgrade_pending",
                "duration_ms": round((time.monotonic() - started) * 1000),
            })
            return deferred
    start_lock = _acquire_start_lock(root)
    if start_lock is not None:
        try:
            _spawn_service(root, port)
        finally:
            shutil.rmtree(start_lock, ignore_errors=True)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        candidate = trusted_service(read_service_record(root), expected_build_id=build_id)
        if candidate is not None:
            (root / PENDING_GENERATION).unlink(missing_ok=True)
            LOGGER.info("local service started", extra={
                "event": "local_service.ensure.completed", "result": "started",
                "duration_ms": round((time.monotonic() - started) * 1000),
            })
            return candidate
        time.sleep(0.05)
    LOGGER.error("local service did not become healthy", extra={
        "event": "local_service.ensure.failed", "result": "failed",
        "duration_ms": round((time.monotonic() - started) * 1000),
    })
    raise RuntimeError("EDGEPILOT_SERVICE_START_FAILED: Live local service did not become healthy")


class ServiceDashboardClient:
    """Dashboard contract consumed by the shared stdio MCP loop."""

    def __init__(self) -> None:
        self.identity: dict[str, Any] | None = None
        self.activation_identity: dict[str, Any] | None = None
        self.error: str | None = None
        self.lease_id: str | None = None
        self._stop = Event()
        self._renewal: Thread | None = None

    def start(self) -> None:
        self.error = None
        try:
            self.identity = ensure_service()
            self.activation_identity = self.identity
            response = service_request(self.identity, "POST", "/api/process/leases/acquire", body={})
            if not isinstance(response, dict) or not isinstance(response.get("lease_id"), str):
                raise RuntimeError("Live local service did not issue a chat lease")
            self.lease_id = response["lease_id"]
            self._renewal = Thread(target=self._renew, name="edgepilot-mcp-lease", daemon=True)
            self._renewal.start()
        except Exception as error:
            self.error = str(error)
            self.identity = None

    def reconcile(self) -> None:
        self.error = None
        try:
            self.activation_identity = reconcile_existing_service()
        except Exception as error:
            self.activation_identity = trusted_service(read_service_record(state_root()))
            self.error = str(error)

    def _renew(self) -> None:
        while not self._stop.wait(10):
            if self.identity is None or self.lease_id is None:
                return
            response = service_request(
                self.identity, "POST", "/api/process/leases/renew", body={"lease_id": self.lease_id},
            )
            if response is None:
                try:
                    self.identity = ensure_service()
                    acquired = service_request(self.identity, "POST", "/api/process/leases/acquire", body={})
                    self.lease_id = str(acquired["lease_id"]) if isinstance(acquired, dict) else None
                except Exception:
                    self.identity = None
                    self.lease_id = None
                    return

    def ensure(self) -> None:
        if self.identity is None or trusted_service(self.identity) is None:
            self.close()
            self._stop = Event()
            self.start()
        if self.identity is not None:
            service_request(self.identity, "POST", "/api/process/browser-handoff", body={})

    def close(self) -> None:
        self._stop.set()
        if self.identity is not None and self.lease_id is not None:
            service_request(
                self.identity, "POST", "/api/process/leases/release", body={"lease_id": self.lease_id},
            )
        self.lease_id = None


def run_service(*, host: str = FIXED_HOST, port: int = FIXED_PORT) -> int:
    service_started = time.monotonic()
    if host != "127.0.0.1":
        raise ValueError("EdgePilot Live service must bind to 127.0.0.1")
    root = state_root()
    nonce = secrets.token_urlsafe(24)
    acquire_service_lock(root, nonce)
    server = None
    try:
        from edgepilot import auth, ui

        build_id = verified_plugin_build_id()
        try:
            server = ui.create_server(host, port)
        except OSError as error:
            if error.errno == errno.EADDRINUSE and port == FIXED_PORT:
                raise RuntimeError(
                    "EDGEPILOT_PORT_IN_USE: 127.0.0.1:8787 is occupied; stop the conflicting program and retry"
                ) from error
            raise
        actual_port = int(server.server_address[1])
        identity = service_identity(host, actual_port, nonce, build_id)
        current_host_version = plugin_host_version()
        state = ServiceState(root, identity, idle_seconds=float(os.environ.get("EDGEPILOT_SERVICE_IDLE_SECONDS", "30")))
        server.edgepilot_identity = identity  # type: ignore[attr-defined]
        server.edgepilot_host_plugin_version = current_host_version  # type: ignore[attr-defined]
        server.edgepilot_service_state = state  # type: ignore[attr-defined]
        ui.configure_job_store(root / "dashboard-jobs", identity)
        _atomic_json(root / SERVICE_RECORD, identity)

        def monitor() -> None:
            while True:
                time.sleep(0.5)
                persistent = (root / PERSISTENT_MARKER).is_file()
                active = ui._all_active_work()
                login_active = auth.dashboard_login_active()
                pending = _read_json(root / PENDING_GENERATION)
                if _pending_requests_upgrade(pending, current_host_version, build_id) and not active:
                    LOGGER.info("local service stopping", extra={
                        "event": "local_service.stopping", "result": "success",
                        "params": {"reason": "upgrade_handoff"},
                    })
                    server.shutdown()
                    return
                if not state.should_stop(external_keepalive=persistent or active or login_active):
                    continue
                LOGGER.info("local service stopping", extra={
                    "event": "local_service.stopping", "result": "success",
                    "params": {"reason": "idle_timeout"},
                })
                server.shutdown()
                return

        Thread(target=monitor, name="edgepilot-service-lifecycle", daemon=True).start()
        LOGGER.info("local service ready", extra={
            "event": "local_service.ready", "result": "success",
            "duration_ms": round((time.monotonic() - service_started) * 1000),
            "params": {"port": actual_port},
        })
        print(identity["url"], flush=True)
        server.serve_forever()
        return 0
    finally:
        if server is not None:
            server.server_close()
        release_service_lock(root, nonce)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the EdgePilot Live local service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--stop-existing", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    if args.stop_existing:
        print(json.dumps(stop_verified_service(force=args.force), sort_keys=True))
        return 0
    if (args.host, args.port) != (FIXED_HOST, FIXED_PORT) \
            and os.environ.get("EDGEPILOT_TEST_ALLOW_EPHEMERAL_PORT") != "1":
        parser.error("EdgePilot Live uses the fixed address 127.0.0.1:8787")
    return run_service(host=args.host, port=args.port)


if __name__ == "__main__":
    raise SystemExit(main())
