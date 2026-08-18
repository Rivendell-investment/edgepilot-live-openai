"""Device login, locked credential storage, refresh, and authenticated HTTP."""

from __future__ import annotations

import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
import subprocess
import sys
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener
from urllib.parse import urlsplit
import webbrowser
import uuid

from filelock import FileLock
import keyring
from keyring.errors import KeyringError, NoKeyringError

from edgepilot.paths import state_root


ORIGIN = os.environ.get("EDGEPILOT_MARKETPLACE_ORIGIN", "https://edge-pilot.rivendell.capital").rstrip("/")
_origin_parts = urlsplit(ORIGIN)
if (_origin_parts.scheme != "https" and not (_origin_parts.scheme == "http" and _origin_parts.hostname in {"127.0.0.1", "localhost", "::1"})) \
        or _origin_parts.path or _origin_parts.query or _origin_parts.fragment or _origin_parts.username or _origin_parts.password:
    raise RuntimeError("EDGEPILOT_MARKETPLACE_ORIGIN must be HTTPS or HTTP loopback origin")
KEYRING_SERVICE = "edgepilot"
PENDING_INSTALLATIONS = "pending_installations.json"


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
    def __init__(self, code: str, *, interval: int | None = None, status: int | None = None):
        super().__init__(code)
        self.code = code
        self.interval = interval
        self.status = status


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
        _secure_windows_acl(path, directory=True)
    return path


def _paths() -> tuple[Path, Path]:
    root = _auth_dir()
    return root / "credentials.json", root / "auth.lock"


def _pending_path() -> Path:
    return _auth_dir() / PENDING_INSTALLATIONS


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


def _windows_sid() -> str:
    result = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True, timeout=5, check=True)
    match = __import__("re").search(r"S-1-[0-9-]+", result.stdout)
    if not match:
        raise AuthError("CREDENTIAL_STORE_ERROR")
    return match.group(0)


def _secure_windows_acl(path: Path, *, directory: bool) -> None:
    if os.name != "nt":
        return
    sid = _windows_sid()
    grant = f"*{sid}:{'(OI)(CI)' if directory else ''}(F)"
    try:
        subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", grant], capture_output=True, timeout=10, check=True)
        checked = subprocess.run(["icacls", str(path), "/verify"], capture_output=True, text=True, timeout=10, check=True)
    except (OSError, subprocess.SubprocessError) as exc:
        raise AuthError("CREDENTIAL_STORE_ERROR") from exc
    if "failed processing 0 files" not in checked.stdout.lower():
        raise AuthError("CREDENTIAL_STORE_ERROR")


def _validate_file(path: Path) -> None:
    if not path.exists():
        return
    if _is_reparse(path) or not path.is_file():
        raise AuthError("CREDENTIAL_STORE_ERROR")
    if os.name != "nt" and (path.stat().st_uid != os.getuid() or stat.S_IMODE(path.stat().st_mode) != 0o600):
        raise AuthError("CREDENTIAL_STORE_ERROR")
    if os.name == "nt":
        _secure_windows_acl(path, directory=False)


@contextmanager
def _credential_lock():
    _, lock_path = _paths()
    if not lock_path.exists():
        try:
            descriptor = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(descriptor)
            if os.name == "nt":
                _secure_windows_acl(lock_path, directory=False)
        except FileExistsError:
            pass
    _validate_file(lock_path)
    with FileLock(str(lock_path)):
        if os.name != "nt" and lock_path.exists():
            lock_path.chmod(0o600)
        _validate_file(lock_path)
        yield
        _validate_file(lock_path)


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
                _secure_windows_acl(temporary, directory=False)
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
            _secure_windows_acl(path, directory=False)
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
    from edgepilot.discovery import strategies_root

    with _credential_lock():
        items = _read_pending_unlocked()
        kept: list[dict[str, Any]] = []
        for item in items:
            if item.get("status") != "prepared":
                kept.append(item)
                continue
            marker = strategies_root() / str(item.get("strategy_slug", "")).replace("-", "_") / ".marketplace.json"
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


def _save_unlocked(path: Path, tokens: dict[str, Any], user: dict[str, Any] | None = None) -> str:
    previous_keyring_user: str | None = None
    _validate_file(path)
    if path.exists():
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            previous = None
        if isinstance(previous, dict) and isinstance(previous.get("keyring_user"), str):
            previous_keyring_user = previous["keyring_user"]
    record = {"access_token": tokens["access_token"], "access_expires_at": time.time() + int(tokens["expires_in"]),
              "session_expires_at": tokens["session_expires_at"], "user": user}
    keyring_user = _keyring_user(user.get("id") if isinstance(user, dict) else None)
    stored_in_keyring = False
    try:
        keyring.set_password(KEYRING_SERVICE, keyring_user, tokens["refresh_token"])
        record.update({"storage": "keyring", "keyring_user": keyring_user})
        stored_in_keyring = True
    except (KeyringError, NoKeyringError):
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
        raise AuthError("CREDENTIAL_STORE_ERROR") from exc
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


def logout(*, all_devices: bool = False, local_only: bool = False) -> dict[str, Any]:
    """Revoke remote credentials before clearing local state.

    A remote failure deliberately leaves local credentials intact so the user can
    retry or explicitly choose local-only cleanup.
    """
    if local_only:
        clear_credentials()
        return {"logged_out": True, "local_only": True, "warning": "Remote tokens may still be valid."}
    path, _ = _paths()
    with _credential_lock():
        credentials = _read_unlocked()
        if not credentials:
            return {"logged_out": True, "all": all_devices}
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
    return {"logged_out": True, "all": all_devices}


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
    if "MARKETPLACE_ADMIN_TOKEN" not in os.environ:
        access_token(interactive=True)
        return
    token = os.environ.get("MARKETPLACE_ADMIN_TOKEN", "")
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
    started, _ = _request("/api/auth/device/start", method="POST", payload={"client_id": "edgepilot-cli", "scope": "edgepilot:use marketplace:install"})
    if open_browser:
        webbrowser.open(str(started["verification_uri_complete"]))
    return started


def poll_login(started: dict[str, Any]) -> dict[str, Any]:
    interval = int(started.get("interval", 5))
    deadline = time.monotonic() + int(started["expires_in"])
    while time.monotonic() < deadline:
        time.sleep(interval)
        try:
            tokens, _ = _request("/api/auth/device/poll", method="POST", payload={"device_code": started["device_code"]})
            profile, _ = _request("/api/me", token=str(tokens["access_token"]))
            user = profile.get("user") if isinstance(profile.get("user"), dict) else None
            if user and isinstance(user.get("id"), int):
                user = {**user, "id": str(user["id"])}
            storage = save_credentials(tokens, user)
            if user and isinstance(user.get("id"), str):
                reconcile_prepared_installations()
                sync_pending_installations(token=str(tokens["access_token"]), user_id=user["id"])
            return {"authenticated": True, "credential_storage": storage, "user": user}
        except AuthError as exc:
            if str(exc) == "authorization_pending":
                continue
            if str(exc) == "slow_down":
                interval = exc.interval if exc.interval is not None else min(60, interval + 5)
                continue
            if str(exc) in {"access_denied", "expired_token"}:
                return {"authenticated": False, "reason": str(exc).upper()}
            raise
    return {"authenticated": False, "reason": "EXPIRED_TOKEN"}


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
