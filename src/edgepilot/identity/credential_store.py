"""Stable key partitioning and failures for Live credential storage."""

from __future__ import annotations

import hashlib

from edgepilot.identity.errors import AuthError


def keyring_user(origin: str, user_id: object = None) -> str:
    origin_partition = hashlib.sha256(origin.encode("utf-8")).hexdigest()[:24]
    account = str(user_id) if isinstance(user_id, (str, int)) and str(user_id) else "unknown"
    return f"{origin_partition}:{account}:refresh-token"


def credential_store_error(stage: str, exc: BaseException | None = None) -> AuthError:
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
