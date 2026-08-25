"""Device login, locked credential storage, refresh, and authenticated HTTP."""

from __future__ import annotations

import base64
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import lru_cache
import hashlib
import json
import logging
import os
from pathlib import Path
import secrets
import stat
import subprocess
import sys
from threading import Lock
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener
from urllib.parse import urlsplit
import webbrowser
import uuid

from edgepilot.file_lock import FileLock
try:
    import keyring
    from keyring.errors import KeyringError, NoKeyringError
except ImportError:  # the lightweight Agent layer intentionally has no keyring dependency
    class KeyringError(Exception):
        pass
    class NoKeyringError(KeyringError):
        pass
    class _MissingKeyring:
        def get_password(self, *_args: object) -> None: raise NoKeyringError("keyring is not installed")
        def set_password(self, *_args: object) -> None: raise NoKeyringError("keyring is not installed")
        def delete_password(self, *_args: object) -> None: raise NoKeyringError("keyring is not installed")
    keyring = _MissingKeyring()  # type: ignore[assignment]

from edgepilot.paths import state_root, strategies_state_root


ORIGIN = os.environ.get("EDGEPILOT_MARKETPLACE_ORIGIN", "https://edge-pilot.rivendell.capital").rstrip("/")
_origin_parts = urlsplit(ORIGIN)
if (_origin_parts.scheme != "https" and not (_origin_parts.scheme == "http" and _origin_parts.hostname in {"127.0.0.1", "localhost", "::1"})) \
        or _origin_parts.path or _origin_parts.query or _origin_parts.fragment or _origin_parts.username or _origin_parts.password:
    raise RuntimeError("EDGEPILOT_MARKETPLACE_ORIGIN must be HTTPS or HTTP loopback origin")
KEYRING_SERVICE = "edgepilot"
PENDING_INSTALLATIONS = "pending_installations.json"
PENDING_LOGIN = "pending-login.json"
PENDING_LOGIN_VERSION = 1
DEVICE_POLL_TIMEOUT_SECONDS = 20
POLL_LEASE_SECONDS = 30
LOGIN_RECEIPT_SECONDS = 60
LOGGER = logging.getLogger("edgepilot.auth")
_WINDOWS_ACL_LOCK = Lock()
_WINDOWS_ACL_FINGERPRINTS: dict[tuple[str, bool], tuple[int, int, int, int, int]] = {}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


_SAFE_OPENER = build_opener(_NoRedirect)


def urlopen(request: Request, *, timeout: int):  # test seam; deliberately rejects redirects
    return _SAFE_OPENER.open(request, timeout=timeout)


def _keyring_user(user_id: object = None) -> str:
    origin_partition = hashlib.sha256(ORIGIN.encode("utf-8")).hexdigest()[:24]
    account = str(user_id) if isinstance(user_id, (str, int)) and str(user_id) else "unknown"
    return f"{origin_partition}:{account}:refresh-token"


class AuthError(RuntimeError):
    def __init__(self, code: str, *, interval: int | None = None, status: int | None = None,
                 stage: str | None = None, diagnostics: dict[str, int | str] | None = None):
        super().__init__(code)
        self.code = code
        self.interval = interval
        self.status = status
        self.stage = stage
        self.diagnostics = diagnostics or {}


def _credential_store_error(stage: str, exc: BaseException | None = None) -> AuthError:
    diagnostics: dict[str, int | str] = {}
    if exc is not None:
        diagnostics["error_type"] = type(exc).__name__
        returncode = getattr(exc, "returncode", None)
        winerror = getattr(exc, "winerror", None)
        if isinstance(returncode, int):
            diagnostics["returncode"] = returncode
        if isinstance(winerror, int):
            diagnostics["winerror"] = winerror
    return AuthError("CREDENTIAL_STORE_ERROR", stage=stage, diagnostics=diagnostics)


def _auth_dir() -> Path:
    requested_root = state_root().expanduser().absolute()
    _validate_requested_chain(requested_root)
    # Canonicalize platform aliases such as macOS /var -> /private/var, while
    # still rejecting a user-controlled state root that is itself a link.
    root = requested_root.resolve(strict=False)
    path = root / "auth"
    if path.exists() and _is_reparse(path):
        raise AuthError("CREDENTIAL_STORE_ERROR")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        path.chmod(0o700)
        if stat.S_IMODE(path.stat().st_mode) != 0o700 or path.stat().st_uid != os.getuid():
            raise AuthError("CREDENTIAL_STORE_ERROR")
    else:
        _secure_windows_acl_once(path, directory=True, stage="auth_dir_acl")
    return path


def _paths() -> tuple[Path, Path]:
    root = _auth_dir()
    return root / "credentials.json", root / "auth.lock"


def _pending_path() -> Path:
    return _auth_dir() / PENDING_INSTALLATIONS


def _pending_login_path() -> Path:
    return _auth_dir() / PENDING_LOGIN


def _read_pending_login_unlocked() -> dict[str, Any] | None:
    path = _pending_login_path()
    _validate_file(path, acl_stage="pending_login_acl")
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        path.unlink(missing_ok=True)
        return None
    if not isinstance(value, dict) or value.get("version") != PENDING_LOGIN_VERSION or value.get("origin") != ORIGIN:
        path.unlink(missing_ok=True)
        return None
    return value


def _write_pending_login_unlocked(value: dict[str, Any]) -> None:
    _atomic_json(_pending_login_path(), value)


def _clear_pending_login_unlocked() -> None:
    path = _pending_login_path()
    _validate_file(path, acl_stage="pending_login_acl")
    path.unlink(missing_ok=True)


def _login_receipt(login_id: str, status_value: str, *, reason: str | None = None,
                   credential_storage: str | None = None, now: float | None = None) -> dict[str, Any]:
    receipt: dict[str, Any] = {
        "version": PENDING_LOGIN_VERSION,
        "origin": ORIGIN,
        "login_id": login_id,
        "status": status_value,
        "completed_at": time.time() if now is None else now,
    }
    if reason:
        receipt["reason"] = reason
    if credential_storage:
        receipt["credential_storage"] = credential_storage
    return receipt


def _public_login_start(value: dict[str, Any], *, now: float | None = None) -> dict[str, Any]:
    current = time.time() if now is None else now
    return {
        "login_id": value["login_id"],
        "verification_uri": value["verification_uri"],
        "verification_uri_complete": value["verification_uri_complete"],
        "user_code": value["user_code"],
        "expires_in": max(0, int(float(value["expires_at"]) - current)),
    }


def _public_login_status(value: dict[str, Any] | None) -> dict[str, Any]:
    if value is None:
        return {"status": "not_found", "reason": "LOGIN_NOT_FOUND"}
    result = {"status": value.get("status", "not_found")}
    if isinstance(value.get("reason"), str):
        result["reason"] = value["reason"]
    if value.get("status") == "authenticated" and value.get("credential_storage") in {"keyring", "file"}:
        result["credential_storage"] = value["credential_storage"]
    return result


def _is_reparse(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return path.is_symlink() or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _validate_requested_chain(path: Path) -> None:
    """Reject user-controlled links in every existing component of a state path."""
    macos_aliases = {Path("/tmp"), Path("/var"), Path("/etc")} if sys.platform == "darwin" else set()
    for component in [*reversed(path.parents), path]:
        if not component.exists() and not component.is_symlink():
            continue
        if _is_reparse(component) and component not in macos_aliases:
            raise AuthError("CREDENTIAL_STORE_ERROR")


@lru_cache(maxsize=1)
def _windows_sid() -> str:
    try:
        result = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True, timeout=5, check=True)
    except (OSError, subprocess.SubprocessError) as exc:
        raise _credential_store_error("windows_sid", exc) from exc
    match = __import__("re").search(r"S-1-[0-9-]+", result.stdout)
    if not match:
        raise _credential_store_error("windows_sid")
    return match.group(0)


def _secure_windows_acl(path: Path, *, directory: bool, stage: str = "windows_acl") -> None:
    if os.name != "nt":
        return
    sid = _windows_sid()
    grant = f"*{sid}:{'(OI)(CI)' if directory else ''}(F)"
    try:
        subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", grant], capture_output=True, timeout=10, check=True)
    except (OSError, subprocess.SubprocessError) as exc:
        raise _credential_store_error(f"{stage}_apply", exc) from exc
    try:
        subprocess.run(["icacls", str(path), "/verify"], capture_output=True, timeout=10, check=True)
    except (OSError, subprocess.SubprocessError) as exc:
        raise _credential_store_error(f"{stage}_verify", exc) from exc


def _secure_windows_acl_once(path: Path, *, directory: bool, stage: str) -> None:
    if os.name != "nt":
        return
    key = (str(path.absolute()), directory)
    with _WINDOWS_ACL_LOCK:
        try:
            info = path.stat()
        except OSError as exc:
            raise _credential_store_error(f"{stage}_stat", exc) from exc
        fingerprint = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        if _WINDOWS_ACL_FINGERPRINTS.get(key) == fingerprint:
            return
        _secure_windows_acl(path, directory=directory, stage=stage)
        try:
            info = path.stat()
        except OSError as exc:
            raise _credential_store_error(f"{stage}_stat", exc) from exc
        _WINDOWS_ACL_FINGERPRINTS[key] = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _validate_file(path: Path, *, acl_stage: str = "credential_acl") -> None:
    if not path.exists():
        return
    if _is_reparse(path) or not path.is_file():
        raise AuthError("CREDENTIAL_STORE_ERROR")
    if os.name != "nt" and (path.stat().st_uid != os.getuid() or stat.S_IMODE(path.stat().st_mode) != 0o600):
        raise AuthError("CREDENTIAL_STORE_ERROR")
    if os.name == "nt":
        _secure_windows_acl_once(path, directory=False, stage=acl_stage)


@contextmanager
def _credential_lock():
    _, lock_path = _paths()
    if not lock_path.exists():
        try:
            descriptor = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(descriptor)
            if os.name == "nt":
                _secure_windows_acl_once(lock_path, directory=False, stage="lock_acl")
        except FileExistsError:
            pass
    _validate_file(lock_path, acl_stage="lock_acl")
    with FileLock(str(lock_path)):
        if os.name != "nt" and lock_path.exists():
            lock_path.chmod(0o600)
        _validate_file(lock_path, acl_stage="lock_acl")
        yield
        _validate_file(lock_path, acl_stage="lock_acl")


def _read_unlocked() -> dict[str, Any] | None:
    path, _ = _paths()
    _validate_file(path)
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AuthError("CREDENTIALS_INVALID") from exc
    if not isinstance(value, dict):
        raise AuthError("CREDENTIALS_INVALID")
    if value.get("storage") == "keyring":
        keyring_user = value.get("keyring_user")
        if not isinstance(keyring_user, str) or not keyring_user.startswith(hashlib.sha256(ORIGIN.encode()).hexdigest()[:24] + ":"):
            raise AuthError("CREDENTIALS_INVALID")
        try:
            value["refresh_token"] = keyring.get_password(KEYRING_SERVICE, keyring_user)
        except (KeyringError, NoKeyringError) as exc:
            raise AuthError("CREDENTIAL_STORE_ERROR") from exc
    return value


def _atomic_json(path: Path, value: dict[str, Any] | list[Any]) -> None:
    temporary = path.parent / f".{path.name}.{secrets.token_hex(8)}.tmp"
    if temporary.exists() or temporary.is_symlink():
        raise AuthError("CREDENTIAL_STORE_ERROR")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        if os.name == "nt":
            try:
                _secure_windows_acl(temporary, directory=False, stage="credential_acl")
            except Exception:
                os.close(descriptor)
                raise
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        _validate_file(path)
        os.replace(temporary, path)
        if os.name != "nt":
            path.chmod(0o600)
        else:
            _secure_windows_acl_once(path, directory=False, stage="credential_acl")
        if os.name != "nt":
            directory_descriptor = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def _read_pending_unlocked() -> list[dict[str, Any]]:
    path = _pending_path()
    _validate_file(path)
    if not path.exists():
        return []
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AuthError("CREDENTIAL_STORE_ERROR") from exc
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise AuthError("CREDENTIAL_STORE_ERROR")
    return value


def _write_pending_unlocked(items: list[dict[str, Any]]) -> None:
    _atomic_json(_pending_path(), items)


def pending_installation_counts() -> dict[str, int]:
    with _credential_lock():
        counts = {state: 0 for state in ("prepared", "installed", "blocked", "failed")}
        for item in _read_pending_unlocked():
            state = item.get("status")
            if state in counts:
                counts[state] += 1
        return counts


def pending_installation_issues(user_id: str) -> list[dict[str, Any]]:
    with _credential_lock():
        return [
            {
                "strategy_slug": item.get("strategy_slug"),
                "strategy_version": item.get("strategy_version"),
                "status": item.get("status"),
                "attempt_count": item.get("attempt_count", 0),
                "next_retry_at": item.get("next_retry_at"),
                "last_error_code": item.get("last_error_code"),
            }
            for item in _read_pending_unlocked()
            if item.get("user_id") == user_id and item.get("status") in {"blocked", "failed"}
        ]


def clear_pending_installations(user_id: str, strategy_slug: str) -> None:
    with _credential_lock():
        items = _read_pending_unlocked()
        _write_pending_unlocked([item for item in items if not (
            item.get("user_id") == user_id and item.get("strategy_slug") == strategy_slug
        )])


def prepare_installation(user_id: str, slug: str, version: str, installed_at: str) -> str:
    """Persist intent before replacing local strategy files."""
    idempotency_key = str(uuid.uuid4())
    item = {
        "user_id": user_id,
        "strategy_slug": slug,
        "strategy_version": version,
        "installed_at": installed_at,
        "idempotency_key": idempotency_key,
        "status": "prepared",
        "attempt_count": 0,
        "next_retry_at": None,
        "last_error_code": None,
    }
    with _credential_lock():
        items = _read_pending_unlocked()
        items.append(item)
        _write_pending_unlocked(items)
    return idempotency_key


def update_pending_installation(idempotency_key: str, status_value: str) -> None:
    if status_value not in {"installed", "blocked", "failed"}:
        raise ValueError("invalid pending installation state")
    with _credential_lock():
        items = _read_pending_unlocked()
        for item in items:
            if item.get("idempotency_key") == idempotency_key:
                item["status"] = status_value
                item["next_retry_at"] = None
                item["last_error_code"] = None
                _write_pending_unlocked(items)
                return
    raise AuthError("INSTALLATION_LOG_MISSING")


def reconcile_prepared_installations() -> None:
    """Promote only intents whose exact Marketplace marker reached disk."""
    with _credential_lock():
        items = _read_pending_unlocked()
        kept: list[dict[str, Any]] = []
        for item in items:
            if item.get("status") != "prepared":
                kept.append(item)
                continue
            marker = strategies_state_root() / str(item.get("strategy_slug", "")).replace("-", "_") / ".marketplace.json"
            try:
                metadata = json.loads(marker.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if metadata.get("slug") == item.get("strategy_slug") and metadata.get("version") == item.get("strategy_version"):
                item["status"] = "installed"
                kept.append(item)
        _write_pending_unlocked(kept)


def sync_pending_installations(*, token: str, user_id: str) -> None:
    """Best-effort receipt upload; local installation success never depends on it."""
    now = time.time()
    with _credential_lock():
        items = _read_pending_unlocked()
        changed = False
        for item in list(items):
            if item.get("user_id") != user_id:
                continue
            if item.get("status") != "installed":
                continue
            retry_at = item.get("next_retry_at")
            if isinstance(retry_at, (int, float)) and retry_at > now:
                continue
            try:
                _request(
                    "/api/account/installations",
                    method="POST",
                    payload={
                        "strategy_slug": item["strategy_slug"],
                        "strategy_version": item["strategy_version"],
                        "installed_at": item["installed_at"],
                    },
                    token=token,
                    idempotency_key=str(item["idempotency_key"]),
                )
            except AuthError as exc:
                if exc.code in {"AUTH_REQUIRED", "INVALID_TOKEN", "HTTP_401"}:
                    try:
                        credentials = _read_unlocked() or {}
                        refreshed = _refresh_unlocked(credentials)
                        token = str(refreshed["access_token"])
                        _request(
                            "/api/account/installations",
                            method="POST",
                            payload={
                                "strategy_slug": item["strategy_slug"],
                                "strategy_version": item["strategy_version"],
                                "installed_at": item["installed_at"],
                            },
                            token=token,
                            idempotency_key=str(item["idempotency_key"]),
                        )
                        items.remove(item)
                        changed = True
                        continue
                    except AuthError as refresh_exc:
                        exc = refresh_exc
                item["attempt_count"] = int(item.get("attempt_count", 0)) + 1
                item["last_error_code"] = exc.code
                if exc.code in {"INSUFFICIENT_SCOPE", "IDEMPOTENCY_CONFLICT"}:
                    item["status"] = "blocked"
                    item["next_retry_at"] = None
                elif (exc.status is not None and 400 <= exc.status < 500) or (
                    exc.code.startswith("HTTP_") and exc.code[5:].isdigit() and 400 <= int(exc.code[5:]) < 500
                ):
                    item["status"] = "failed"
                    item["next_retry_at"] = None
                else:
                    # Deterministic bounded exponential backoff plus small jitter.
                    delay = min(24 * 3600, 60 * (2 ** min(10, item["attempt_count"] - 1)))
                    item["next_retry_at"] = now + delay + secrets.randbelow(max(1, delay // 5 + 1))
                changed = True
                continue
            items.remove(item)
            changed = True
        if changed:
            _write_pending_unlocked(items)


def _save_unlocked(path: Path, tokens: dict[str, Any], user: dict[str, Any] | None = None,
                   *, login_id: str | None = None) -> str:
    previous_keyring_user: str | None = None
    previous: dict[str, Any] | None = None
    _validate_file(path)
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            previous = loaded if isinstance(loaded, dict) else None
        except (OSError, json.JSONDecodeError):
            previous = None
        if isinstance(previous, dict) and isinstance(previous.get("keyring_user"), str):
            previous_keyring_user = previous["keyring_user"]
    record = {"access_token": tokens["access_token"], "access_expires_at": time.time() + int(tokens["expires_in"]),
              "session_expires_at": tokens["session_expires_at"], "user": user}
    durable_login_id = login_id or tokens.get("login_id") or (previous or {}).get("login_id")
    if isinstance(durable_login_id, str) and durable_login_id:
        record["login_id"] = durable_login_id
    keyring_user = _keyring_user(user.get("id") if isinstance(user, dict) else None)
    stored_in_keyring = False
    try:
        keyring.set_password(KEYRING_SERVICE, keyring_user, tokens["refresh_token"])
        record.update({"storage": "keyring", "keyring_user": keyring_user})
        stored_in_keyring = True
    except (KeyringError, NoKeyringError) as exc:
        LOGGER.warning("credential keyring unavailable", extra={"event": "auth.credentials.keyring_fallback", "result": "fallback",
                       "params": {"stage": "keyring_write", "error_type": type(exc).__name__}})
        record.update({"storage": "file", "refresh_token": tokens["refresh_token"]})
    try:
        _atomic_json(path, record)
    except Exception as exc:
        # A newly created account entry must not become an orphan when the
        # durable metadata write fails. For an in-place refresh, retain the
        # entry because the existing metadata still points to the same key.
        if stored_in_keyring and previous_keyring_user != keyring_user:
            try:
                keyring.delete_password(KEYRING_SERVICE, keyring_user)
            except (KeyringError, NoKeyringError):
                pass
        if isinstance(exc, AuthError):
            raise
        raise _credential_store_error("metadata_replace", exc) from exc
    if previous_keyring_user and previous_keyring_user != record.get("keyring_user"):
        try:
            keyring.delete_password(KEYRING_SERVICE, previous_keyring_user)
        except (KeyringError, NoKeyringError):
            pass
    return str(record["storage"])


def save_credentials(tokens: dict[str, Any], user: dict[str, Any] | None = None) -> str:
    path, _ = _paths()
    with _credential_lock():
        return _save_unlocked(path, tokens, user)


def _clear_unlocked(path: Path, credentials: dict[str, Any]) -> None:
    path.unlink(missing_ok=True)
    try:
        if isinstance(credentials.get("keyring_user"), str):
            keyring.delete_password(KEYRING_SERVICE, credentials["keyring_user"])
    except (KeyringError, NoKeyringError):
        pass


def clear_credentials() -> None:
    path, _ = _paths()
    with _credential_lock():
        _validate_file(path)
        credentials: dict[str, Any] = {}
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    credentials = raw
            except (OSError, json.JSONDecodeError):
                pass
        _clear_unlocked(path, credentials)
        _clear_pending_login_unlocked()


def logout(*, all_devices: bool = False, local_only: bool = False) -> dict[str, Any]:
    """Revoke remote credentials before clearing local state.

    A remote failure deliberately leaves local credentials intact so the user can
    retry or explicitly choose local-only cleanup.
    """
    if local_only:
        clear_credentials()
        return {
            "logged_out": True,
            "local_only": True,
            "warning": "Remote tokens or pending device authorizations may still be valid.",
        }
    path, _ = _paths()
    with _credential_lock():
        pending = _load_login_state_unlocked(time.time())
        canceled_pending = False
        if pending is not None and pending.get("status") == "pending":
            device_code = pending.get("device_code")
            if not isinstance(device_code, str) or not device_code:
                raise AuthError("CREDENTIALS_INVALID")
            _request("/api/auth/device/cancel", method="POST", payload={"device_code": device_code})
            canceled_pending = True
            _write_pending_login_unlocked(
                _login_receipt(str(pending["login_id"]), "denied", reason="ACCESS_DENIED")
            )
        credentials = _read_unlocked()
        if not credentials:
            _clear_pending_login_unlocked()
            result = {"logged_out": True, "all": all_devices}
            if canceled_pending:
                result["canceled_pending_login"] = True
            return result
        if all_devices:
            token = credentials.get("access_token")
            if not isinstance(token, str) or float(credentials.get("access_expires_at", 0)) <= time.time():
                token = str(_refresh_unlocked(credentials)["access_token"])
            _request("/api/auth/logout-all", method="POST", token=token)
        else:
            refresh = credentials.get("refresh_token")
            if not isinstance(refresh, str) or not refresh:
                raise AuthError("CREDENTIALS_INVALID")
            _request("/api/auth/logout", method="POST", payload={"refresh_token": refresh})
        _clear_unlocked(path, credentials)
        _clear_pending_login_unlocked()
    result = {"logged_out": True, "all": all_devices}
    if canceled_pending:
        result["canceled_pending_login"] = True
    return result


def _request(path: str, *, method: str = "GET", payload: dict[str, Any] | None = None, token: str | None = None,
             idempotency_key: str | None = None, timeout: int = 20) -> tuple[dict[str, Any], dict[str, str]]:
    headers = {"Accept": "application/json", "User-Agent": "EdgePilot CLI/1.0"}
    data = None
    if payload is not None:
        data = json.dumps(payload, separators=(",", ":")).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    try:
        with urlopen(Request(f"{ORIGIN}{path}", data=data, method=method, headers=headers), timeout=timeout) as response:
            raw = response.read()
            return (json.loads(raw) if raw else {}), {key.lower(): value for key, value in response.headers.items()}
    except HTTPError as exc:
        try:
            error = json.loads(exc.read())
        except (json.JSONDecodeError, UnicodeDecodeError):
            error = {}
        envelope = error.get("error", {})
        code = envelope.get("code", f"HTTP_{exc.code}")
        interval = envelope.get("interval")
        raise AuthError(str(code), interval=interval if isinstance(interval, int) else None, status=exc.code) from exc
    except URLError as exc:
        raise AuthError("AUTH_SERVICE_UNAVAILABLE") from exc


def _refresh_unlocked(credentials: dict[str, Any]) -> dict[str, Any]:
    refresh = credentials.get("refresh_token")
    if not isinstance(refresh, str) or not refresh:
        raise AuthError("CREDENTIALS_INVALID")
    tokens, _ = _request("/api/auth/token/refresh", method="POST", payload={"refresh_token": refresh})
    path, _ = _paths()
    storage = _save_unlocked(path, tokens, credentials.get("user"))
    tokens["storage"] = storage
    return tokens


def refresh_access_token() -> str:
    path, _ = _paths()
    with _credential_lock():
        credentials = _read_unlocked()
        if not credentials:
            raise AuthError("AUTH_REQUIRED")
        try:
            tokens = _refresh_unlocked(credentials)
        except AuthError as exc:
            if exc.code in {"invalid_grant", "INVALID_TOKEN", "ACCOUNT_DISABLED", "CREDENTIALS_INVALID"}:
                _clear_unlocked(path, credentials)
            raise
        return str(tokens["access_token"])


def authenticated_request(path: str, *, method: str = "GET", payload: dict[str, Any] | None = None,
                          idempotency_key: str | None = None, timeout: int = 20) -> tuple[dict[str, Any], dict[str, str]]:
    """Send one authenticated request and replay once only when it is safe."""
    safe_to_replay = method.upper() in {"GET", "HEAD", "OPTIONS", "PUT", "DELETE"} or idempotency_key is not None
    token = access_token()
    try:
        return _request(path, method=method, payload=payload, token=token, idempotency_key=idempotency_key, timeout=timeout)
    except AuthError as exc:
        if not safe_to_replay or exc.code not in {"AUTH_REQUIRED", "INVALID_TOKEN", "HTTP_401"}:
            raise
    return _request(path, method=method, payload=payload, token=refresh_access_token(), idempotency_key=idempotency_key, timeout=timeout)


def skip_auth_enabled() -> bool:
    """Return True when local research may bypass Marketplace login.

    Set ``EDGEPILOT_SKIP_AUTH=1`` (also accepts ``true`` / ``yes`` / ``on``) to skip
    product authentication for CLI commands such as ``backtest`` and ``data``.
    This does not grant Marketplace install or remote admin privileges.
    """
    value = os.environ.get("EDGEPILOT_SKIP_AUTH", "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def authorize_backtest() -> None:
    """Authorize a backtest with an explicit admin token or the normal user session."""
    if skip_auth_enabled():
        return
    if "SUPER_ADMIN_TOKEN" not in os.environ:
        access_token(interactive=True)
        return
    token = os.environ.get("SUPER_ADMIN_TOKEN", "")
    try:
        decoded = base64.b64decode(token, validate=True)
    except (ValueError, TypeError) as exc:
        raise AuthError("ADMIN_TOKEN_INVALID") from exc
    if base64.b64encode(decoded).decode() != token or len(decoded) < 32:
        raise AuthError("ADMIN_TOKEN_INVALID")
    try:
        result, _ = _request("/api/admin/auth/check", token=token)
    except AuthError as exc:
        if exc.status in {401, 403}:
            raise AuthError("ADMIN_TOKEN_INVALID", status=exc.status) from exc
        raise
    if result != {"authenticated": True, "actor": "marketplace_admin"}:
        raise AuthError("ADMIN_AUTH_INVALID")


def status() -> dict[str, Any]:
    try:
        path, _ = _paths()
        with _credential_lock():
            credentials = _read_unlocked()
            if not credentials:
                return {"authenticated": False, "reason": "NO_CREDENTIALS"}
            if float(credentials.get("access_expires_at", 0)) <= time.time():
                try:
                    _refresh_unlocked(credentials)
                    credentials = _read_unlocked() or {}
                except AuthError as exc:
                    if str(exc) in {"invalid_grant", "INVALID_TOKEN", "ACCOUNT_DISABLED", "CREDENTIALS_INVALID"}:
                        _clear_unlocked(path, credentials)
                    reason = {
                        "ACCOUNT_DISABLED": "ACCOUNT_DISABLED",
                        "AUTH_SERVICE_UNAVAILABLE": "AUTH_SERVICE_UNAVAILABLE",
                        "CREDENTIAL_STORE_ERROR": "CREDENTIAL_STORE_ERROR",
                        "CREDENTIALS_INVALID": "CREDENTIALS_INVALID",
                    }.get(str(exc), "REFRESH_REJECTED")
                    return {"authenticated": False, "reason": reason}
            result = {"authenticated": True, "user": credentials.get("user"), "access_expires_at": credentials.get("access_expires_at"), "credential_storage": credentials.get("storage")}
        user = result.get("user")
        if isinstance(user, dict) and isinstance(user.get("id"), str):
            try:
                reconcile_prepared_installations()
                sync_pending_installations(token=str(credentials["access_token"]), user_id=user["id"])
            except AuthError:
                pass
        result["pending_installations"] = pending_installation_counts()
        return result
    except AuthError as exc:
        return {"authenticated": False, "reason": str(exc)}


def _load_login_state_unlocked(now: float) -> dict[str, Any] | None:
    value = _read_pending_login_unlocked()
    if value is None:
        return None
    status_value = value.get("status")
    if status_value != "pending":
        completed_at = value.get("completed_at")
        if not isinstance(completed_at, (int, float)) or now - float(completed_at) > LOGIN_RECEIPT_SECONDS:
            _clear_pending_login_unlocked()
            return None
        return value
    required = {
        "login_id": str, "device_code": str, "user_code": str,
        "verification_uri": str, "verification_uri_complete": str,
        "interval": (int, float), "created_at": (int, float),
        "expires_at": (int, float), "next_poll_at": (int, float),
    }
    if any(not isinstance(value.get(key), expected) for key, expected in required.items()):
        _clear_pending_login_unlocked()
        return None
    try:
        credentials = _read_unlocked()
    except AuthError as exc:
        if exc.code in {"CREDENTIAL_STORE_ERROR", "CREDENTIALS_INVALID"}:
            receipt = _login_receipt(str(value["login_id"]), "failed", reason="CREDENTIAL_STORE_ERROR", now=now)
            _write_pending_login_unlocked(receipt)
            return receipt
        raise
    if credentials and credentials.get("login_id") == value["login_id"]:
        storage = credentials.get("storage")
        if storage in {"keyring", "file"} and isinstance(credentials.get("refresh_token"), str):
            receipt = _login_receipt(str(value["login_id"]), "authenticated",
                                     credential_storage=str(storage), now=now)
            _write_pending_login_unlocked(receipt)
            return receipt
    if float(value["expires_at"]) <= now:
        receipt = _login_receipt(str(value["login_id"]), "expired", reason="EXPIRED_TOKEN", now=now)
        _write_pending_login_unlocked(receipt)
        return receipt
    return value


def login_status(login_id: str) -> dict[str, Any]:
    with _credential_lock():
        value = _load_login_state_unlocked(time.time())
        if value is None or value.get("login_id") != login_id:
            return _public_login_status(None)
        return _public_login_status(value)


def resume_login() -> dict[str, Any] | None:
    """Return the current public pending flow so a restarted owner can resume it."""
    with _credential_lock():
        value = _load_login_state_unlocked(time.time())
        if value is None or value.get("status") != "pending":
            return None
        return _public_login_start(value)


def login(*, open_browser: bool = True) -> dict[str, Any]:
    started = start_login(open_browser=open_browser)
    print(
        "EdgePilot login required. Open " + str(started.get("verification_uri", ""))
        + " and enter one-time code " + str(started.get("user_code", "")) + ".",
        file=sys.stderr,
        flush=True,
    )
    return poll_login(started)


def start_login(*, open_browser: bool = True) -> dict[str, Any]:
    now = time.time()
    with _credential_lock():
        existing = _load_login_state_unlocked(now)
        if existing is not None and existing.get("status") == "pending":
            public = _public_login_start(existing, now=now)
        else:
            public = None
    if public is None:
        started, _ = _request(
            "/api/auth/device/start",
            method="POST",
            payload={"client_id": "edgepilot-cli", "scope": "edgepilot:use marketplace:install"},
        )
        string_fields = ("device_code", "user_code", "verification_uri", "verification_uri_complete")
        if any(not isinstance(started.get(key), str) or not started[key] for key in string_fields) \
                or not isinstance(started.get("expires_in"), int):
            raise AuthError("PROTOCOL_ERROR")
        with _credential_lock():
            now = time.time()
            existing = _load_login_state_unlocked(now)
            if existing is not None and existing.get("status") == "pending":
                public = _public_login_start(existing, now=now)
            else:
                interval = max(1, int(started.get("interval", 5)))
                expires_in = max(1, int(started["expires_in"]))
                value = {
                    "version": PENDING_LOGIN_VERSION,
                    "origin": ORIGIN,
                    "login_id": uuid.uuid4().hex,
                    "status": "pending",
                    "device_code": str(started["device_code"]),
                    "user_code": str(started["user_code"]),
                    "verification_uri": str(started["verification_uri"]),
                    "verification_uri_complete": str(started["verification_uri_complete"]),
                    "interval": interval,
                    "created_at": now,
                    "expires_at": now + expires_in,
                    "next_poll_at": now + interval,
                    "lease_owner": None,
                    "lease_expires_at": None,
                }
                _write_pending_login_unlocked(value)
                public = _public_login_start(value, now=now)
    if open_browser:
        webbrowser.open(str(public["verification_uri_complete"]))
    return public


def _revoke_late_login(tokens: dict[str, Any]) -> None:
    refresh = tokens.get("refresh_token")
    if not isinstance(refresh, str) or not refresh:
        return
    try:
        _request(
            "/api/auth/logout",
            method="POST",
            payload={"refresh_token": refresh},
            timeout=DEVICE_POLL_TIMEOUT_SECONDS,
        )
    except AuthError as exc:
        LOGGER.warning(
            "late device login revocation failed",
            extra={"event": "auth.device.late_revoke", "result": "failed", "params": {"code": exc.code}},
        )


def _poll_result(value: dict[str, Any]) -> dict[str, Any]:
    if value.get("status") == "authenticated":
        return {"authenticated": True, "credential_storage": value.get("credential_storage")}
    return {"authenticated": False, "reason": value.get("reason", "LOGIN_NOT_FOUND")}


def poll_login(started: dict[str, Any]) -> dict[str, Any]:
    login_id = started.get("login_id")
    if not isinstance(login_id, str) or not login_id:
        raise AuthError("PROTOCOL_ERROR")
    owner = uuid.uuid4().hex
    while True:
        wait_for = 0.0
        with _credential_lock():
            now = time.time()
            value = _load_login_state_unlocked(now)
            if value is None or value.get("login_id") != login_id:
                return _poll_result(_public_login_status(None))
            if value.get("status") != "pending":
                return _poll_result(value)
            next_poll_at = float(value["next_poll_at"])
            lease_owner = value.get("lease_owner")
            lease_expires_at = float(value.get("lease_expires_at") or 0)
            if lease_owner and lease_owner != owner and lease_expires_at > now:
                wait_for = max(next_poll_at, lease_expires_at) - now
            elif next_poll_at > now:
                wait_for = next_poll_at - now
            else:
                value["lease_owner"] = owner
                value["lease_expires_at"] = now + POLL_LEASE_SECONDS
                value["next_poll_at"] = now + float(value["interval"])
                device_code = str(value["device_code"])
                _write_pending_login_unlocked(value)
        if wait_for > 0:
            time.sleep(wait_for)
            continue
        try:
            tokens, _ = _request(
                "/api/auth/device/poll",
                method="POST",
                payload={"device_code": device_code},
                timeout=DEVICE_POLL_TIMEOUT_SECONDS,
            )
        except AuthError as exc:
            with _credential_lock():
                response_at = time.time()
                value = _load_login_state_unlocked(response_at)
                if value is None or value.get("login_id") != login_id or value.get("status") != "pending":
                    return _poll_result(_public_login_status(None) if value is None else value)
                if value.get("lease_owner") != owner:
                    continue
                if exc.code in {"authorization_pending", "slow_down"}:
                    if exc.code == "slow_down":
                        value["interval"] = exc.interval if exc.interval is not None else min(60, int(value["interval"]) + 5)
                    value["next_poll_at"] = response_at + float(value["interval"])
                    value["lease_owner"] = None
                    value["lease_expires_at"] = None
                    _write_pending_login_unlocked(value)
                    continue
                if exc.code in {"access_denied", "expired_token"}:
                    receipt = _login_receipt(login_id, "denied" if exc.code == "access_denied" else "expired",
                                             reason=exc.code.upper(), now=response_at)
                else:
                    reason = exc.code if exc.code in {"AUTH_SERVICE_UNAVAILABLE", "CREDENTIAL_STORE_ERROR"} else "PROTOCOL_ERROR"
                    receipt = _login_receipt(login_id, "failed", reason=reason, now=response_at)
                _write_pending_login_unlocked(receipt)
                return _poll_result(receipt)

        late = False
        try:
            with _credential_lock():
                value = _load_login_state_unlocked(time.time())
                if value is None or value.get("login_id") != login_id or value.get("status") != "pending":
                    late = True
                else:
                    path, _ = _paths()
                    storage = _save_unlocked(path, tokens, login_id=login_id)
                    saved = _read_unlocked()
                    if not saved or saved.get("login_id") != login_id or not isinstance(saved.get("refresh_token"), str):
                        raise AuthError("CREDENTIAL_STORE_ERROR")
                    receipt = _login_receipt(login_id, "authenticated", credential_storage=storage)
                    _write_pending_login_unlocked(receipt)
        except AuthError:
            with _credential_lock():
                value = _read_pending_login_unlocked()
                if value is not None and value.get("login_id") == login_id:
                    receipt = _login_receipt(login_id, "failed", reason="CREDENTIAL_STORE_ERROR")
                    _write_pending_login_unlocked(receipt)
            _revoke_late_login(tokens)
            return {"authenticated": False, "reason": "CREDENTIAL_STORE_ERROR"}
        if late:
            _revoke_late_login(tokens)
            return _poll_result(_public_login_status(None))

        user: dict[str, Any] | None = None
        try:
            profile, _ = _request("/api/me", token=str(tokens["access_token"]))
            user = profile.get("user") if isinstance(profile.get("user"), dict) else None
            if user and isinstance(user.get("id"), int):
                user = {**user, "id": str(user["id"])}
            if user and isinstance(user.get("id"), str):
                with _credential_lock():
                    current = _read_unlocked()
                    if current and current.get("login_id") == login_id:
                        _save_unlocked(_paths()[0], tokens, user, login_id=login_id)
                reconcile_prepared_installations()
                sync_pending_installations(token=str(tokens["access_token"]), user_id=user["id"])
        except AuthError:
            pass
        return {"authenticated": True, "credential_storage": storage, "user": user}


def access_token(*, interactive: bool = False) -> str:
    current = status()
    if not current.get("authenticated") and interactive:
        current = login()
    if not current.get("authenticated"):
        raise AuthError("AUTH_REQUIRED")
    path, _ = _paths()
    with _credential_lock():
        credentials = _read_unlocked() or {}
        token = credentials.get("access_token")
        if not isinstance(token, str):
            raise AuthError("CREDENTIALS_INVALID")
        return token
