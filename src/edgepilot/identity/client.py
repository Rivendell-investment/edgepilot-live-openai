"""Bounded, redirect-safe HTTP transport for Marketplace Identity."""

from __future__ import annotations

import json
import re
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request

from edgepilot.identity.errors import AuthError


UrlOpen = Callable[..., Any]


def request(
    origin: str,
    path: str,
    *,
    urlopen: UrlOpen,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    token: str | None = None,
    idempotency_key: str | None = None,
    timeout: int = 20,
    max_response_bytes: int = 1024 * 1024,
    max_error_bytes: int = 16 * 1024,
) -> tuple[dict[str, Any], dict[str, str]]:
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
        request_value = Request(
            f"{origin}{path}",
            data=data,
            method=method,
            headers=headers,
        )
        with urlopen(request_value, timeout=timeout) as response:
            raw = response.read(max_response_bytes + 1)
            if len(raw) > max_response_bytes:
                raise AuthError("PROTOCOL_ERROR")
            try:
                parsed = json.loads(raw) if raw else {}
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise AuthError("PROTOCOL_ERROR") from exc
            if not isinstance(parsed, dict):
                raise AuthError("PROTOCOL_ERROR")
            return parsed, {
                key.lower(): value for key, value in response.headers.items()
            }
    except HTTPError as exc:
        try:
            raw = exc.read(max_error_bytes + 1)
            error = json.loads(raw) if len(raw) <= max_error_bytes and raw else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            error = {}
        envelope = error.get("error") if isinstance(error, dict) else None
        if isinstance(envelope, dict):
            code = envelope.get("code", f"HTTP_{exc.code}")
            interval = envelope.get("interval")
        elif isinstance(envelope, str) and re.fullmatch(
            r"[A-Za-z][A-Za-z0-9_]{0,63}",
            envelope,
        ):
            code = envelope
            interval = None
        else:
            code = (
                error.get("code", f"HTTP_{exc.code}")
                if isinstance(error, dict)
                else f"HTTP_{exc.code}"
            )
            interval = error.get("interval") if isinstance(error, dict) else None
        raise AuthError(
            str(code),
            interval=interval if isinstance(interval, int) else None,
            status=exc.code,
        ) from exc
    except (URLError, TimeoutError, ConnectionError) as exc:
        raise AuthError("AUTH_SERVICE_UNAVAILABLE") from exc
