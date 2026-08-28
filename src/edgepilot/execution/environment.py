from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlsplit

import msgspec

from nautilus_trader.common.config import resolve_path

from edgepilot.strategies.discovery import AdapterDescriptor
from edgepilot.platform.env_file import load_env


# Native adapters name their demo/test environment differently.  EdgePilot's
# generic "demo" mode must select whichever member the adapter's environment
# enum actually defines (for example DigiFinex exposes TEST, not DEMO).
_DEMO_ENVIRONMENT_ALIASES = {
    "DIGIFINEX": "TEST",
}


def adapter_environment(adapter_name: str, mode: str) -> str:
    """Return the native adapter environment name for an EdgePilot trading mode."""
    if mode == "paper":
        return "LIVE"
    if mode == "demo":
        return _DEMO_ENVIRONMENT_ALIASES.get(adapter_name.upper(), "DEMO")
    return mode.upper()


# Nautilus reaches a venue over two transports with different proxy behaviour:
# the Rust HTTP client is built on reqwest and picks up the standard proxy
# variables on its own, while the Rust websocket client only tunnels through a
# proxy that was passed to it explicitly.  Left alone that splits a run in half
# — REST calls succeed through the proxy while the market-data and execution
# websockets try to reach the venue directly.  Resolving one value here and
# handing it to the native configs keeps both transports on the same route.
_PROXY_VARIABLES = (
    "EDGEPILOT_PROXY_URL",
    "https_proxy",
    "HTTPS_PROXY",
    "http_proxy",
    "HTTP_PROXY",
)


def _config_fields(config_path: str) -> set[str]:
    """Return the field names a native Nautilus config class accepts."""
    return {field.name for field in msgspec.structs.fields(resolve_path(config_path))}


def resolve_proxy_url() -> str | None:
    """Return the account-wide outbound proxy, or None when traffic goes direct.

    ``EDGEPILOT_PROXY_URL`` is EdgePilot's own setting and is stored alongside
    the account's API keys, so a value that cannot be used is reported instead
    of being dropped — silently not proxying is the failure this setting exists
    to prevent.  The standard proxy variables are shared with the rest of the
    shell and may name schemes this transport cannot tunnel (SOCKS, for
    example), so unusable values there are skipped rather than raised.

    ``no_proxy`` is deliberately not interpreted: the native clients are
    configured before any destination host is known, and honouring per-host
    exclusions here would mean guessing which endpoints an adapter will reach.
    """
    for name in _PROXY_VARIABLES:
        value = os.environ.get(name, "").strip()
        if not value:
            continue
        parsed = urlsplit(value)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            return value
        if name == "EDGEPILOT_PROXY_URL":
            raise ValueError(
                f"{name} must be an http:// or https:// URL, found: {value}",
            )
    return None


def apply_proxy_url(options: dict[str, Any], config_path: str) -> None:
    """Route a native client through the account proxy unless it names its own.

    Adapters that do not expose ``proxy_url`` are left untouched, so a proxy
    configured for the account cannot turn into an unexpected keyword argument
    for a venue that has no proxy support.
    """
    if "proxy_url" not in _config_fields(config_path):
        return
    proxy_url = resolve_proxy_url()
    if proxy_url:
        options.setdefault("proxy_url", proxy_url)


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
    fields = _config_fields(config_path)
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
    fields = _config_fields(config_path)
    values = {}
    for requirement in credential_requirements(adapter, mode):
        field = requirement["field"]
        value = os.environ.get(requirement["environment_variable"])
        if field in fields and value:
            values[field] = value
    return values
