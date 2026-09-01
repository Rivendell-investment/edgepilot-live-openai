"""Account-owned Strategy Workspace configuration persistence."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any

from edgepilot.platform.file_lock import FileLock
from edgepilot.platform.paths import strategy_configurations_state_root


CONFIGURATION_SCHEMA_VERSION = 1
MAX_CONFIGURATION_BYTES = 32 * 1024
_NAME = re.compile(r"[a-z][a-z0-9_-]{0,79}")
_STRATEGY = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,79}")


class ConfigurationConflictError(ValueError):
    """Raised when create/update would overwrite a different revision."""


class ConfigurationCorruptError(ValueError):
    """Raised when stored configuration state cannot be trusted."""


def validate_configuration_name(value: str) -> str:
    if not _NAME.fullmatch(value):
        raise ValueError("configuration names use lowercase letters, numbers, underscores, and hyphens")
    return value


def validate_strategy_name(value: str) -> str:
    if not _STRATEGY.fullmatch(value):
        raise ValueError("invalid strategy name")
    return value


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _secure_directory(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise ValueError("configuration state directory cannot be a symlink")
    if os.name != "nt":
        path.chmod(0o700)
    return path


def _strategy_directory(strategy: str) -> Path:
    root = _secure_directory(strategy_configurations_state_root())
    directory = root / validate_strategy_name(strategy)
    if directory.exists() and directory.is_symlink():
        raise ValueError("strategy configuration directory cannot be a symlink")
    return _secure_directory(directory)


def _configuration_path(strategy: str, name: str) -> Path:
    directory = _strategy_directory(strategy)
    path = directory / f"{validate_configuration_name(name)}.json"
    if path.exists() and path.is_symlink():
        raise ValueError("configuration file cannot be a symlink")
    return path


def _lock_path(strategy: str) -> Path:
    return _strategy_directory(strategy) / ".configurations.lock"


def _validate_record(value: Any, *, strategy: str, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigurationCorruptError("configuration record must be a JSON object")
    required = {
        "schema_version", "strategy", "name", "base_preset", "base_config_sha256",
        "target_id", "settings", "revision", "created_at", "updated_at",
    }
    if set(value) != required:
        raise ConfigurationCorruptError("configuration record fields are invalid")
    if value.get("schema_version") != CONFIGURATION_SCHEMA_VERSION:
        raise ConfigurationCorruptError("configuration schema version is unsupported")
    if value.get("strategy") != strategy or value.get("name") != name:
        raise ConfigurationCorruptError("configuration identity is inconsistent")
    if not isinstance(value.get("base_preset"), str) or not _NAME.fullmatch(value["base_preset"]):
        raise ConfigurationCorruptError("configuration base preset is invalid")
    digest = value.get("base_config_sha256")
    if not isinstance(digest, str) or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ConfigurationCorruptError("configuration base digest is invalid")
    if not isinstance(value.get("target_id"), str) or not value["target_id"]:
        raise ConfigurationCorruptError("configuration target is invalid")
    if not isinstance(value.get("settings"), dict):
        raise ConfigurationCorruptError("configuration settings are invalid")
    revision = value.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise ConfigurationCorruptError("configuration revision is invalid")
    if not all(isinstance(value.get(field), str) and value[field] for field in ("created_at", "updated_at")):
        raise ConfigurationCorruptError("configuration timestamps are invalid")
    return dict(value)


def _read_unlocked(path: Path, *, strategy: str, name: str) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(name)
    if path.stat().st_size > MAX_CONFIGURATION_BYTES:
        raise ConfigurationCorruptError("configuration file is too large")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConfigurationCorruptError("configuration file is unreadable") from exc
    return _validate_record(value, strategy=strategy, name=name)


def load_configuration(strategy: str, name: str) -> dict[str, Any] | None:
    path = _configuration_path(strategy, name)
    with FileLock(str(_lock_path(strategy))):
        if not path.exists():
            return None
        return _read_unlocked(path, strategy=strategy, name=name)


def list_configuration_names(strategy: str) -> list[str]:
    directory = _strategy_directory(strategy)
    with FileLock(str(_lock_path(strategy))):
        names = []
        for path in directory.glob("*.json"):
            if path.is_file() and not path.is_symlink() and _NAME.fullmatch(path.stem):
                names.append(path.stem)
        return sorted(names)


def _write_atomic(path: Path, value: dict[str, Any]) -> None:
    payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if len(payload) > MAX_CONFIGURATION_BYTES:
        raise ValueError("configuration exceeds the local size limit")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name != "nt":
            temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def create_configuration(strategy: str, name: str, values: dict[str, Any]) -> dict[str, Any]:
    path = _configuration_path(strategy, name)
    with FileLock(str(_lock_path(strategy))):
        if path.exists():
            raise ConfigurationConflictError(f"configuration already exists: {name}")
        now = _utc_now()
        record = {
            "schema_version": CONFIGURATION_SCHEMA_VERSION,
            "strategy": validate_strategy_name(strategy),
            "name": validate_configuration_name(name),
            "base_preset": values.get("base_preset"),
            "base_config_sha256": values.get("base_config_sha256"),
            "target_id": values.get("target_id"),
            "settings": values.get("settings"),
            "revision": 1,
            "created_at": now,
            "updated_at": now,
        }
        record = _validate_record(record, strategy=strategy, name=name)
        _write_atomic(path, record)
        return record


def update_configuration(
    strategy: str,
    name: str,
    values: dict[str, Any],
    *,
    expected_revision: int,
) -> dict[str, Any]:
    path = _configuration_path(strategy, name)
    with FileLock(str(_lock_path(strategy))):
        current = _read_unlocked(path, strategy=strategy, name=name)
        if current["revision"] != expected_revision:
            raise ConfigurationConflictError(
                f"configuration changed: expected revision {expected_revision}, current revision {current['revision']}",
            )
        record = {
            **current,
            "base_preset": values.get("base_preset"),
            "base_config_sha256": values.get("base_config_sha256"),
            "target_id": values.get("target_id"),
            "settings": values.get("settings"),
            "revision": current["revision"] + 1,
            "updated_at": _utc_now(),
        }
        record = _validate_record(record, strategy=strategy, name=name)
        _write_atomic(path, record)
        return record


def delete_configuration(strategy: str, name: str, *, expected_revision: int | None = None) -> None:
    path = _configuration_path(strategy, name)
    with FileLock(str(_lock_path(strategy))):
        current = _read_unlocked(path, strategy=strategy, name=name)
        if expected_revision is not None and current["revision"] != expected_revision:
            raise ConfigurationConflictError(
                f"configuration changed: expected revision {expected_revision}, current revision {current['revision']}",
            )
        path.unlink()
