"""Privacy-bounded product behavior event delivery."""
from __future__ import annotations

import json
import os
import uuid
from typing import Any
from urllib.request import Request

from edgepilot import __version__, auth
from edgepilot.marketplace_client.client import urlopen
from edgepilot.marketplace_client.origin import marketplace_origin
from edgepilot.platform.paths import state_root


def _installation_id() -> str:
    path = state_root() / "behavior-installation-id"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        return str(uuid.UUID(path.read_text(encoding="ascii").strip()))
    except (OSError, ValueError):
        value = uuid.uuid4()
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return str(uuid.UUID(path.read_text(encoding="ascii").strip()))
    with os.fdopen(descriptor, "w", encoding="ascii") as stream:
        stream.write(str(value))
    return str(value)


def record_behavior_event(payload: dict[str, Any]) -> None:
    body = {**payload, "installation_id": _installation_id(), "client_version": __version__}
    if auth.status().get("authenticated"):
        auth.authenticated_request("/api/live/behavior-events", method="POST", payload=body, timeout=5)
        return
    request = Request(f"{marketplace_origin()}/api/live/behavior-events",
        data=json.dumps(body, separators=(",", ":")).encode("utf-8"), method="POST",
        headers={"Accept": "application/json", "Content-Type": "application/json", "User-Agent": f"edgepilot/{__version__}"})
    with urlopen(request, timeout=5) as response:
        response.read(4097)
