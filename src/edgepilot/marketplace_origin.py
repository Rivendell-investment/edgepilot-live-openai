"""Resolve the Live Marketplace origin after local environment loading."""

from __future__ import annotations

import os
from urllib.parse import urlsplit


DEFAULT_MARKETPLACE_ORIGIN = "https://edge-pilot.rivendell.capital"


def marketplace_origin() -> str:
    """Return a validated Marketplace origin from the current process environment."""
    origin = os.environ.get("EDGEPILOT_MARKETPLACE_ORIGIN", DEFAULT_MARKETPLACE_ORIGIN).rstrip("/")
    parts = urlsplit(origin)
    is_allowed_scheme = parts.scheme == "https" or (
        parts.scheme == "http" and parts.hostname in {"127.0.0.1", "localhost", "::1"}
    )
    if (
        not is_allowed_scheme
        or not parts.netloc
        or parts.path
        or parts.query
        or parts.fragment
        or parts.username
        or parts.password
    ):
        raise RuntimeError("EDGEPILOT_MARKETPLACE_ORIGIN must be HTTPS or HTTP loopback origin")
    return origin
