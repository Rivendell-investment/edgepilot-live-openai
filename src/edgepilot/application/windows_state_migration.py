"""Recoverable Windows migration from the legacy AppData Live state root."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from typing import Any

from edgepilot.platform.file_lock import FileLock


MIGRATION_RECORD = "windows-state-migration.json"
MIGRATION_LOCK = "windows-state-migration.lock"
PERSISTENT_REGISTRATION_PENDING = "background-dashboard/migration-registration.pending"
MIGRATED_PATHS = (
    "auth",
    "accounts",
    "strategies",
    ".env",
    "catalog",
    "behavior-installation-id",
    "background-dashboard/enabled.json",
)


class WindowsStateMigrationConflict(RuntimeError):
    """The legacy root cannot be moved without overwriting current state."""


def migration_required(source: Path, destination: Path) -> bool:
    source = source.expanduser().absolute()
    destination = destination.expanduser().absolute()
    if _same_path(source, destination):
        return False
    return any(_exists(source / relative) for relative in MIGRATED_PATHS)


def migrate_windows_state(source: Path, destination: Path) -> dict[str, Any]:
    """Move selected state atomically per entry, resuming after interruption."""
    source = source.expanduser().absolute()
    destination = destination.expanduser().absolute()
    if _same_path(source, destination):
        return {"migrated": False, "reason": "SAME_ROOT", "entries": []}
    _validate_root(source, "source")
    _validate_root(destination, "destination")
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    with FileLock(str(destination / MIGRATION_LOCK)):
        return _migrate_locked(source, destination)


def persistent_registration_pending(destination: Path) -> bool:
    return (destination / PERSISTENT_REGISTRATION_PENDING).is_file()


def complete_persistent_registration(destination: Path) -> None:
    (destination / PERSISTENT_REGISTRATION_PENDING).unlink(missing_ok=True)


def _migrate_locked(source: Path, destination: Path) -> dict[str, Any]:
    for relative in MIGRATED_PATHS:
        _validate_entry_path(source, relative, "source")
        _validate_entry_path(destination, relative, "destination")
    record_path = destination / MIGRATION_RECORD
    record = _read_record(record_path)
    if record is None:
        entries = [relative for relative in MIGRATED_PATHS if _exists(source / relative)]
        if not entries:
            return {"migrated": False, "reason": "NO_LEGACY_STATE", "entries": []}
        conflicts = [relative for relative in MIGRATED_PATHS if _exists(destination / relative)]
        if conflicts:
            raise WindowsStateMigrationConflict(
                "WINDOWS_STATE_MIGRATION_CONFLICT: destination already contains "
                + ", ".join(conflicts)
            )
        record = {
            "schema_version": 1,
            "source_root": str(source),
            "phase": "moving",
            "entries": entries,
            "moved": [],
        }
        _atomic_json(record_path, record)
    else:
        _validate_record(record, source)
        if record["phase"] == "complete":
            return {"migrated": False, "reason": "ALREADY_MIGRATED", "entries": record["entries"]}

    moved = list(record["moved"])
    try:
        for relative in record["entries"]:
            source_path = source / relative
            destination_path = destination / relative
            source_exists = _exists(source_path)
            destination_exists = _exists(destination_path)
            if source_exists and destination_exists:
                raise WindowsStateMigrationConflict(
                    f"WINDOWS_STATE_MIGRATION_CONFLICT: both roots contain {relative}"
                )
            if not source_exists and not destination_exists:
                raise WindowsStateMigrationConflict(
                    f"WINDOWS_STATE_MIGRATION_INCOMPLETE: both roots are missing {relative}"
                )
            if source_exists:
                destination_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                _move_path(source_path, destination_path)
            if relative not in moved:
                moved.append(relative)
                record["moved"] = moved
                _atomic_json(record_path, record)
        if "background-dashboard/enabled.json" in record["entries"]:
            pending = destination / PERSISTENT_REGISTRATION_PENDING
            _atomic_json(pending, {"schema_version": 1, "source_root": str(source)})
        record["phase"] = "complete"
        _atomic_json(record_path, record)
    except Exception:
        _rollback(source, destination, record, record_path)
        raise
    return {"migrated": True, "reason": "MIGRATED", "entries": record["entries"]}


def _rollback(source: Path, destination: Path, record: dict[str, Any], record_path: Path) -> None:
    failures: list[str] = []
    for relative in reversed(record["entries"]):
        source_path = source / relative
        destination_path = destination / relative
        if _exists(destination_path) and not _exists(source_path):
            try:
                source_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                _move_path(destination_path, source_path)
            except OSError:
                failures.append(relative)
        elif _exists(destination_path) and _exists(source_path):
            failures.append(relative)
    if failures:
        record["phase"] = "recovery_required"
        record["rollback_failures"] = failures
        _atomic_json(record_path, record)
        raise WindowsStateMigrationConflict(
            "WINDOWS_STATE_MIGRATION_RECOVERY_REQUIRED: " + ", ".join(failures)
        )
    (destination / PERSISTENT_REGISTRATION_PENDING).unlink(missing_ok=True)
    record_path.unlink(missing_ok=True)


def _validate_record(record: dict[str, Any], source: Path) -> None:
    required = {"schema_version", "source_root", "phase", "entries", "moved"}
    if set(record) - {"rollback_failures"} != required \
            or record.get("schema_version") != 1 \
            or record.get("source_root") != str(source) \
            or record.get("phase") not in {"moving", "complete", "recovery_required"} \
            or not isinstance(record.get("entries"), list) \
            or not isinstance(record.get("moved"), list) \
            or any(item not in MIGRATED_PATHS for item in record["entries"]) \
            or any(item not in record["entries"] for item in record["moved"]):
        raise WindowsStateMigrationConflict("WINDOWS_STATE_MIGRATION_RECORD_INVALID")


def _read_record(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise WindowsStateMigrationConflict("WINDOWS_STATE_MIGRATION_RECORD_INVALID") from error
    if not isinstance(value, dict):
        raise WindowsStateMigrationConflict("WINDOWS_STATE_MIGRATION_RECORD_INVALID")
    return value


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
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


def _validate_root(path: Path, label: str) -> None:
    if _is_link(path):
        raise WindowsStateMigrationConflict(f"WINDOWS_STATE_MIGRATION_UNSAFE_{label.upper()}_ROOT")


def _validate_entry_path(root: Path, relative: str, label: str) -> None:
    current = root
    for part in Path(relative).parts:
        current /= part
        if _is_link(current):
            raise WindowsStateMigrationConflict(
                f"WINDOWS_STATE_MIGRATION_UNSAFE_{label.upper()}_ENTRY: {relative}"
            )


def _is_link(path: Path) -> bool:
    is_junction = getattr(os.path, "isjunction", None)
    return path.is_symlink() or bool(is_junction(path) if is_junction is not None else False)


def _exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _same_path(first: Path, second: Path) -> bool:
    return os.path.normcase(str(first)) == os.path.normcase(str(second))


def _move_path(source: Path, destination: Path) -> None:
    source.replace(destination)
