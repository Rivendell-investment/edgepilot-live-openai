"""Durable, idempotent Marketplace installation receipt outbox."""

from __future__ import annotations

import json
from pathlib import Path
import secrets
from threading import Lock
import time
from typing import Any, Callable, ContextManager
import uuid

from edgepilot.identity.errors import AuthError


ReadItems = Callable[[], list[dict[str, Any]]]
WriteItems = Callable[[list[dict[str, Any]]], None]
LockFactory = Callable[[], ContextManager[Any]]
Request = Callable[..., tuple[dict[str, Any], dict[str, str]]]
_SYNC_LOCK = Lock()


def pending_counts(
    user_id: str | None,
    *,
    lock: LockFactory,
    read_items: ReadItems,
) -> dict[str, int]:
    with lock():
        counts = {state: 0 for state in ("prepared", "installed", "blocked", "failed")}
        for item in read_items():
            if user_id is not None and item.get("user_id") != user_id:
                continue
            state = item.get("status")
            if state in counts:
                counts[state] += 1
        return counts


def pending_issues(
    user_id: str,
    *,
    lock: LockFactory,
    read_items: ReadItems,
) -> list[dict[str, Any]]:
    with lock():
        return [
            {
                "strategy_slug": item.get("strategy_slug"),
                "strategy_version": item.get("strategy_version"),
                "status": item.get("status"),
                "attempt_count": item.get("attempt_count", 0),
                "next_retry_at": item.get("next_retry_at"),
                "last_error_code": item.get("last_error_code"),
            }
            for item in read_items()
            if item.get("user_id") == user_id
            and item.get("status") in {"blocked", "failed"}
        ]


def clear_pending(
    user_id: str,
    strategy_slug: str,
    *,
    lock: LockFactory,
    read_items: ReadItems,
    write_items: WriteItems,
) -> None:
    with lock():
        items = read_items()
        write_items(
            [
                item
                for item in items
                if not (
                    item.get("user_id") == user_id
                    and item.get("strategy_slug") == strategy_slug
                )
            ],
        )


def prepare(
    user_id: str,
    slug: str,
    version: str,
    installed_at: str,
    *,
    lock: LockFactory,
    read_items: ReadItems,
    write_items: WriteItems,
) -> str:
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
    with lock():
        items = read_items()
        items.append(item)
        write_items(items)
    return idempotency_key


def update(
    idempotency_key: str,
    status_value: str,
    *,
    lock: LockFactory,
    read_items: ReadItems,
    write_items: WriteItems,
) -> None:
    if status_value not in {"installed", "blocked", "failed"}:
        raise ValueError("invalid pending installation state")
    with lock():
        items = read_items()
        for item in items:
            if item.get("idempotency_key") == idempotency_key:
                item["status"] = status_value
                item["next_retry_at"] = None
                item["last_error_code"] = None
                write_items(items)
                return
    raise AuthError("INSTALLATION_LOG_MISSING")


def reconcile_prepared(
    user_id: str | None,
    strategies_root: Path,
    *,
    lock: LockFactory,
    read_items: ReadItems,
    write_items: WriteItems,
) -> None:
    with lock():
        items = read_items()
        kept: list[dict[str, Any]] = []
        changed = False
        for item in items:
            if user_id is not None and item.get("user_id") != user_id:
                kept.append(item)
                continue
            if item.get("status") != "prepared":
                kept.append(item)
                continue
            marker = (
                strategies_root
                / str(item.get("strategy_slug", "")).replace("-", "_")
                / ".marketplace.json"
            )
            try:
                metadata = json.loads(marker.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                changed = True
                continue
            if (
                metadata.get("slug") == item.get("strategy_slug")
                and metadata.get("version") == item.get("strategy_version")
            ):
                kept.append({**item, "status": "installed"})
                changed = True
            else:
                changed = True
        if changed:
            write_items(kept)


def sync(
    *,
    token: str,
    user_id: str,
    lock: LockFactory,
    read_items: ReadItems,
    write_items: WriteItems,
    request: Request,
    read_credentials: Callable[[], dict[str, Any] | None],
    refresh_credentials: Callable[[dict[str, Any]], dict[str, Any]],
) -> None:
    """Best-effort receipt upload; local installation success never depends on it."""
    with _SYNC_LOCK:
        _sync_once(
            token=token,
            user_id=user_id,
            lock=lock,
            read_items=read_items,
            write_items=write_items,
            request=request,
            read_credentials=read_credentials,
            refresh_credentials=refresh_credentials,
        )


def _sync_once(
    *,
    token: str,
    user_id: str,
    lock: LockFactory,
    read_items: ReadItems,
    write_items: WriteItems,
    request: Request,
    read_credentials: Callable[[], dict[str, Any] | None],
    refresh_credentials: Callable[[dict[str, Any]], dict[str, Any]],
) -> None:
    now = time.time()
    with lock():
        candidates = [
            dict(item)
            for item in read_items()
            if item.get("user_id") == user_id
            and item.get("status") == "installed"
            and not (
                isinstance(item.get("next_retry_at"), (int, float))
                and item["next_retry_at"] > now
            )
        ]
    for item in candidates:
        payload = {
            "strategy_slug": item["strategy_slug"],
            "strategy_version": item["strategy_version"],
            "installed_at": item["installed_at"],
        }
        try:
            request(
                "/api/account/installations",
                method="POST",
                payload=payload,
                token=token,
                idempotency_key=str(item["idempotency_key"]),
            )
        except AuthError as exc:
            if exc.code in {"AUTH_REQUIRED", "INVALID_TOKEN", "HTTP_401"}:
                try:
                    credentials = read_credentials() or {}
                    refreshed = refresh_credentials(credentials)
                    token = str(refreshed["access_token"])
                    request(
                        "/api/account/installations",
                        method="POST",
                        payload=payload,
                        token=token,
                        idempotency_key=str(item["idempotency_key"]),
                    )
                    _remove_installed(
                        str(item["idempotency_key"]),
                        lock=lock,
                        read_items=read_items,
                        write_items=write_items,
                    )
                    continue
                except AuthError as refresh_exc:
                    exc = refresh_exc
            _record_failure(
                str(item["idempotency_key"]),
                exc,
                now=now,
                lock=lock,
                read_items=read_items,
                write_items=write_items,
            )
            continue
        _remove_installed(
            str(item["idempotency_key"]),
            lock=lock,
            read_items=read_items,
            write_items=write_items,
        )


def _remove_installed(
    idempotency_key: str,
    *,
    lock: LockFactory,
    read_items: ReadItems,
    write_items: WriteItems,
) -> None:
    with lock():
        items = read_items()
        kept = [
            item
            for item in items
            if not (
                item.get("idempotency_key") == idempotency_key
                and item.get("status") == "installed"
            )
        ]
        if len(kept) != len(items):
            write_items(kept)


def _record_failure(
    idempotency_key: str,
    exc: AuthError,
    *,
    now: float,
    lock: LockFactory,
    read_items: ReadItems,
    write_items: WriteItems,
) -> None:
    with lock():
        items = read_items()
        for index, current in enumerate(items):
            if (
                current.get("idempotency_key") != idempotency_key
                or current.get("status") != "installed"
            ):
                continue
            item = dict(current)
            item["attempt_count"] = int(item.get("attempt_count", 0)) + 1
            item["last_error_code"] = exc.code
            if exc.code in {"INSUFFICIENT_SCOPE", "IDEMPOTENCY_CONFLICT"}:
                item["status"] = "blocked"
                item["next_retry_at"] = None
            elif (exc.status is not None and 400 <= exc.status < 500) or (
                exc.code.startswith("HTTP_")
                and exc.code[5:].isdigit()
                and 400 <= int(exc.code[5:]) < 500
            ):
                item["status"] = "failed"
                item["next_retry_at"] = None
            else:
                attempt_count = int(item["attempt_count"])
                delay = min(24 * 3600, 60 * (2 ** min(10, attempt_count - 1)))
                item["next_retry_at"] = now + delay + secrets.randbelow(
                    max(1, delay // 5 + 1),
                )
            items[index] = item
            write_items(items)
            return
