from __future__ import annotations

import os
from typing import Any

import msgspec

from nautilus_trader.common.config import resolve_path

from edgepilot.discovery import AdapterDescriptor
from edgepilot.env_file import load_env


_CREDENTIAL_LABELS = {
    "api_key": "API key",
    "api_secret": "API secret",
    "api_passphrase": "API passphrase",
    "passphrase": "passphrase",
    "username": "username",
    "password": "password",
    "private_key": "private key",
}


def credential_requirements(
    adapter: AdapterDescriptor,
    mode: str = "live",
) -> list[dict[str, Any]]:
    """Inspect the native config and report mode-scoped credential variables."""
    if mode not in {"paper", "demo", "live"}:
        raise ValueError(f"Unsupported credential mode: {mode}")
    config_path = adapter.data_config_path if mode == "paper" else adapter.exec_config_path
    if config_path is None:
        return []
    config_cls = resolve_path(config_path)
    fields = {field.name for field in msgspec.structs.fields(config_cls)}
    requirements = []
    for name, label in _CREDENTIAL_LABELS.items():
        if name not in fields:
            continue
        environment_variable = f"{adapter.name}_{mode.upper()}_{name.upper()}"
        requirements.append(
            {
                "field": name,
                "label": label,
                "environment_variable": environment_variable,
                "required": mode != "paper",
                "configured": bool(os.environ.get(environment_variable)),
            },
        )
    return requirements


def credential_options(
    adapter: AdapterDescriptor,
    mode: str,
    config_path: str,
) -> dict[str, str]:
    """Return only credentials supported by a specific native config class."""
    config_cls = resolve_path(config_path)
    fields = {field.name for field in msgspec.structs.fields(config_cls)}
    values = {}
    for requirement in credential_requirements(adapter, mode):
        field = requirement["field"]
        value = os.environ.get(requirement["environment_variable"])
        if field in fields and value:
            values[field] = value
    return values
