"""Stable user-owned paths for EdgePilot runtime state."""

from __future__ import annotations

import os
from contextvars import ContextVar, Token
from hashlib import sha256
from pathlib import Path
from typing import Iterator


ACCOUNT_KEY_ENV = "EDGEPILOT_ACCOUNT_KEY"
_ACCOUNT_CONTEXT: ContextVar[str | None] = ContextVar("edgepilot_account_key", default=None)


def state_root() -> Path:
    """Return persistent state independently of the plugin cache location."""
    configured = os.environ.get("EDGEPILOT_HOME")
    if configured:
        return Path(configured).expanduser()
    if os.name == "nt":
        app_data = os.environ.get("APPDATA")
        return (Path(app_data) if app_data else Path.home()) / "EdgePilot"
    return Path.home() / ".edgepilot"


def account_key_for_user(user_id: str, origin: str) -> str:
    """Return a stable, non-PII local namespace for one Marketplace user."""
    normalized_user_id = user_id.strip()
    normalized_origin = origin.rstrip("/")
    if not normalized_user_id or not normalized_origin:
        raise ValueError("user id and Marketplace origin are required")
    return sha256(f"{normalized_origin}\0{normalized_user_id}".encode()).hexdigest()


def activate_account(user_id: str, origin: str) -> str:
    """Activate the only account visible to this local process."""
    key = account_key_for_user(user_id, origin)
    os.environ[ACCOUNT_KEY_ENV] = key
    _ACCOUNT_CONTEXT.set(key)
    return key


def deactivate_account() -> None:
    os.environ.pop(ACCOUNT_KEY_ENV, None)
    _ACCOUNT_CONTEXT.set(None)


def bind_account_key(account_key: str | None) -> Token[str | None]:
    """Pin one request or worker to its authenticated account."""
    if account_key is not None and not _valid_account_key(account_key):
        raise ValueError("invalid account key")
    return _ACCOUNT_CONTEXT.set(account_key)


def clear_bound_account_key() -> None:
    """Discard a context left by a previous keep-alive request thread."""
    _ACCOUNT_CONTEXT.set(None)


def reset_account_key(token: Token[str | None]) -> None:
    _ACCOUNT_CONTEXT.reset(token)


def _valid_account_key(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def active_account_key() -> str | None:
    bound = _ACCOUNT_CONTEXT.get()
    if bound is not None:
        return bound
    value = os.environ.get(ACCOUNT_KEY_ENV, "")
    return value if _valid_account_key(value) else None


def account_state_root() -> Path:
    """Return the active account state, or legacy state for explicit auth bypass tests."""
    key = active_account_key()
    if not key:
        return state_root()
    root = state_root() / "accounts" / key
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        root.chmod(0o700)
    return root


def account_credentials_path() -> Path:
    return account_state_root() / ".env"


def strategies_state_root() -> Path:
    """Return the user-owned strategy package directory."""
    return account_state_root() / "strategies"


def strategy_runs_path(strategy_name: str) -> Path:
    """Return a strategy's own persistent run directory."""
    if not strategy_name or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for character in strategy_name):
        raise ValueError("invalid strategy name")
    path = strategies_state_root() / strategy_name / "runs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def iter_run_directories() -> Iterator[Path]:
    """Yield every persisted run, grouped beneath its owning strategy."""
    root = strategies_state_root()
    if not root.exists():
        return
    for strategy_path in root.iterdir():
        runs = strategy_path / "runs"
        if not strategy_path.is_dir() or not runs.is_dir():
            continue
        for candidate in runs.iterdir():
            if candidate.is_dir() and (candidate / "run.json").is_file():
                yield candidate


def find_run_directory(run_id: str) -> Path:
    """Find a globally unique run ID without introducing a global runs folder."""
    matches = [path for path in iter_run_directories() if path.name == run_id]
    if len(matches) != 1:
        raise FileNotFoundError(f"Unknown run: {run_id}")
    return matches[0]
