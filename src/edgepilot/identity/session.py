"""Access-token refresh and safe authenticated request replay policy."""

from __future__ import annotations

from typing import Any, Callable

from edgepilot.identity.errors import AuthError


Request = Callable[..., tuple[dict[str, Any], dict[str, str]]]


def authenticated_request(
    path: str,
    *,
    method: str,
    payload: dict[str, Any] | None,
    idempotency_key: str | None,
    timeout: int,
    access_token: Callable[[], str],
    refresh_access_token: Callable[[], str],
    request: Request,
) -> tuple[dict[str, Any], dict[str, str]]:
    safe_to_replay = (
        method.upper() in {"GET", "HEAD", "OPTIONS", "PUT", "DELETE"}
        or idempotency_key is not None
    )
    token = access_token()
    try:
        return request(
            path,
            method=method,
            payload=payload,
            token=token,
            idempotency_key=idempotency_key,
            timeout=timeout,
        )
    except AuthError as exc:
        if not safe_to_replay or exc.code not in {
            "AUTH_REQUIRED",
            "INVALID_TOKEN",
            "HTTP_401",
        }:
            raise
    return request(
        path,
        method=method,
        payload=payload,
        token=refresh_access_token(),
        idempotency_key=idempotency_key,
        timeout=timeout,
    )
