"""In-memory ownership for the active Dashboard login flow."""

from __future__ import annotations

from threading import Lock
import time
from typing import Any

from edgepilot.identity.errors import AuthError


FLOW_LOCK = Lock()
START_LOCK = Lock()
FLOWS: dict[str, dict[str, Any]] = {}


def flow(login_id: str) -> dict[str, Any]:
    with FLOW_LOCK:
        value = FLOWS.get(login_id)
        if not value or float(value.get("expires_at", 0)) <= time.time():
            FLOWS.pop(login_id, None)
            raise AuthError("LOGIN_EXPIRED")
        return dict(value)


def login_active() -> bool:
    now = time.time()
    with FLOW_LOCK:
        expired = [
            login_id
            for login_id, value in FLOWS.items()
            if float(value.get("expires_at", 0)) <= now
        ]
        for login_id in expired:
            FLOWS.pop(login_id, None)
        return bool(FLOWS)
