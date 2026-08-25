"""Structured application logging with safe daily, per-process files."""

from __future__ import annotations

from datetime import date, datetime, timedelta
import json
import logging
import os
from pathlib import Path
import re
import sys
from threading import RLock
from typing import Any

from edgepilot.paths import iter_run_directories, state_root


APP_LOG_RE = re.compile(r"^edgepilot-(\d{4}-\d{2}-\d{2})-p\d+\.log$")
SENSITIVE_NAMES = {"password", "passwd", "pwd", "apikey", "apisecret", "secret", "passphrase", "token", "authorization", "cookie", "privatekey", "accesskey", "secretkey"}
SECRET_IN_TEXT = re.compile(r"(password|passwd|pwd|api[_-]?(?:key|secret)|secret|passphrase|token|authorization)(\s*[=:]\s*)([^\s,;]+)", re.IGNORECASE)
MAX_LOG_BYTES = 32 * 1024
_configured_path: Path | None = None


def redact(value: Any, *, _seen: set[int] | None = None) -> Any:
    """Return a bounded, recursively redacted logging value."""
    seen = _seen if _seen is not None else set()
    if isinstance(value, str):
        cleaned = SECRET_IN_TEXT.sub(r"\1\2[REDACTED]", value)
        return cleaned if len(cleaned.encode("utf-8")) <= MAX_LOG_BYTES // 2 else cleaned[:1024] + "…[TRUNCATED]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, (list, tuple, set)):
        return [redact(item, _seen=seen) for item in list(value)[:20]]
    if isinstance(value, dict):
        identity = id(value)
        if identity in seen:
            return "[CIRCULAR]"
        seen.add(identity)
        return {
            str(key): "[REDACTED]" if _sensitive_name(str(key)) else redact(item, _seen=seen)
            for key, item in value.items()
        }
    return redact(str(value), _seen=seen)


def _sensitive_name(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", key.lower())
    return any(normalized == name or normalized.endswith(name) for name in SENSITIVE_NAMES)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created).astimezone().isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "service": "edgepilot",
            "event": getattr(record, "event", "application.message"),
            "message": redact(record.getMessage()),
        }
        for name in ("request_id", "job_id", "run_id", "client_ip", "duration_ms", "result", "params"):
            value = getattr(record, name, None)
            if value is not None:
                payload[name] = redact(value)
        if record.exc_info:
            payload["error"] = redact({
                "type": record.exc_info[0].__name__ if record.exc_info[0] else "Exception",
                "message": str(record.exc_info[1]),
                "stack": self.formatException(record.exc_info),
            })
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
        if len(encoded.encode("utf-8")) > MAX_LOG_BYTES:
            payload["params"] = "[TRUNCATED]"
            if isinstance(payload.get("error"), dict):
                payload["error"]["stack"] = "[TRUNCATED]"
            encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
        if len(encoded.encode("utf-8")) > MAX_LOG_BYTES:
            encoded = json.dumps({
                "timestamp": payload["timestamp"], "level": payload["level"],
                "service": "edgepilot", "event": payload["event"],
                "message": "[TRUNCATED]",
            }, separators=(",", ":"))
        return encoded


class FallbackFileHandler(logging.FileHandler):
    def handleError(self, record: logging.LogRecord) -> None:  # noqa: N802
        try:
            sys.stderr.write(self.format(record) + "\n")
            sys.stderr.flush()
        except Exception:
            pass


class DailyPidFileHandler(logging.Handler):
    def __init__(self, directory: Path) -> None:
        super().__init__()
        self.directory = directory
        self._date: date | None = None
        self._handler: logging.FileHandler | None = None
        self._lock = RLock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            with self._lock:
                today = datetime.now().astimezone().date()
                if today != self._date:
                    if self._handler:
                        self._handler.close()
                    self.directory.mkdir(parents=True, exist_ok=True)
                    path = self.directory / f"edgepilot-{today.isoformat()}-p{os.getpid()}.log"
                    self._handler = FallbackFileHandler(path, encoding="utf-8", delay=False)
                    self._handler.setFormatter(self.formatter)
                    self._date = today
                    cleanup_logs(self.directory, today=today)
                assert self._handler is not None
                self._handler.emit(record)
        except Exception:
            try:
                sys.stderr.write(self.format(record) + "\n")
                sys.stderr.flush()
            except Exception:
                pass

    def setFormatter(self, formatter: logging.Formatter | None) -> None:  # noqa: N802
        super().setFormatter(formatter)
        if self._handler:
            self._handler.setFormatter(formatter)

    def close(self) -> None:
        with self._lock:
            if self._handler:
                self._handler.close()
                self._handler = None
        super().close()


def cleanup_logs(directory: Path, *, today: date | None = None) -> None:
    """Delete only expired EdgePilot and inactive Nautilus log files."""
    current = today or datetime.now().astimezone().date()
    cutoff = current - timedelta(days=29)
    if directory.exists():
        try:
            paths = list(directory.iterdir())
        except OSError:
            paths = []
        for path in paths:
            try:
                match = APP_LOG_RE.match(path.name)
                if match and date.fromisoformat(match.group(1)) < cutoff:
                    path.unlink(missing_ok=True)
            except OSError:
                continue
    try:
        run_directories = list(iter_run_directories())
    except OSError:
        run_directories = []
    for run_dir in run_directories:
        target = run_dir / "nautilus.log"
        if not target.is_file() or _run_is_active(run_dir):
            continue
        try:
            modified = datetime.fromtimestamp(target.stat().st_mtime).astimezone().date()
            if modified < cutoff:
                target.unlink(missing_ok=True)
        except OSError:
            continue


def _run_is_active(run_dir: Path) -> bool:
    running_path = run_dir.parent / "running.json"
    try:
        stored = json.loads(running_path.read_text(encoding="utf-8"))
        pid = int(stored.get(run_dir.name, 0))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except (OSError, ProcessLookupError):
        return False
    return True


def configure_logging(*, directory: Path | None = None, level: str | None = None) -> logging.Logger:
    """Configure EdgePilot logging once for the requested directory."""
    global _configured_path
    target = directory or Path(os.environ.get("EDGEPILOT_LOG_DIR", state_root() / "logs")).expanduser()
    logger = logging.getLogger("edgepilot")
    if _configured_path == target and logger.handlers:
        logger.setLevel((level or os.environ.get("EDGEPILOT_LOG_LEVEL", "INFO")).upper())
        return logger
    for handler in list(logger.handlers):
        handler.close()
        logger.removeHandler(handler)
    handler = DailyPidFileHandler(target)
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    logger.setLevel((level or os.environ.get("EDGEPILOT_LOG_LEVEL", "INFO")).upper())
    logger.propagate = False
    _configured_path = target
    cleanup_logs(target)
    return logger


def shutdown_logging() -> None:
    """Flush, close, and detach EdgePilot-owned logging handlers."""
    global _configured_path
    logger = logging.getLogger("edgepilot")
    for handler in list(logger.handlers):
        try:
            handler.flush()
        finally:
            handler.close()
            logger.removeHandler(handler)
    _configured_path = None
