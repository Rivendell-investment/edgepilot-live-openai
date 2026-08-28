"""Stable localhost Dashboard response and error mapping.

This module deliberately contains no product state or native runtime imports.
The HTTP handler supplies the concrete response writer so the transport remains
owned by :mod:`edgepilot.ui` while error contracts can be tested independently.
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler
from typing import Any, Callable

from edgepilot import auth
from edgepilot.marketplace_client.client import MarketplaceRequestError


ErrorWriter = Callable[..., None]


MARKETPLACE_ERROR_RESPONSES = {
    "INVALID_ARGUMENT": (400, "the recommendation request is invalid"),
    "INVALID_REQUEST": (400, "the recommendation request is invalid"),
    "AUTH_REQUIRED": (401, "authentication is required"),
    "INVALID_TOKEN": (401, "authentication has expired"),
    "INSUFFICIENT_SCOPE": (403, "permission to use recommendations is required"),
    "CATALOG_COVERAGE_INSUFFICIENT": (409, "the strategy catalog cannot provide three compatible recommendations"),
    "RATE_LIMITED": (429, "too many recommendation requests; try again later"),
    "DOWNLOAD_QUOTA_EXCEEDED": (429, "strategy download quota exceeded"),
    "DOWNLOAD_QUOTA_UNAVAILABLE": (503, "strategy download quota service is unavailable"),
    "SERVICE_UNAVAILABLE": (503, "the recommendation service is unavailable"),
    "AUTH_SERVICE_UNAVAILABLE": (503, "the authentication service is unavailable"),
}

AUTH_ERROR_RESPONSES = {
    "INVALID_REQUEST": (400, "the login request is invalid", False),
    "INVALID_EMAIL_CODE": (400, "the code is invalid or expired", False),
    "AUTH_REQUIRED": (401, "the login flow expired", False),
    "LOGIN_EXPIRED": (410, "the login flow expired", False),
    "LOGIN_NOT_READY": (409, "the login could not be completed", True),
    "GOOGLE_CREDENTIAL_INVALID": (401, "Google sign-in was invalid or expired", False),
    "GOOGLE_PROVIDER_UNAVAILABLE": (503, "Google sign-in is temporarily unavailable", True),
    "ACCOUNT_SWITCH_REQUIRES_LOGOUT": (409, "sign out before using another account", False),
    "RATE_LIMITED": (429, "too many login requests; try again later", True),
    "SERVER_UPDATE_REQUIRED": (503, "Marketplace Server must be updated before Dashboard login can be used", False),
    "AUTH_SERVICE_UNAVAILABLE": (503, "the authentication service is unavailable", True),
    "CREDENTIAL_STORE_ERROR": (503, "the local credential store is unavailable", True),
    "PROTOCOL_ERROR": (502, "the authentication service returned an invalid response", True),
}


def marketplace_request_error(
    handler: BaseHTTPRequestHandler,
    exc: MarketplaceRequestError,
    write_error: ErrorWriter,
) -> None:
    status, message = MARKETPLACE_ERROR_RESPONSES.get(
        exc.code,
        (500, "the recommendation request failed"),
    )
    code = exc.code if exc.code in MARKETPLACE_ERROR_RESPONSES else "INTERNAL_ERROR"
    write_error(handler, code, message, status, **exc.public_details())


def auth_request_error(
    handler: BaseHTTPRequestHandler,
    exc: auth.AuthError,
    write_error: ErrorWriter,
) -> None:
    status, message, retryable = AUTH_ERROR_RESPONSES.get(
        exc.code,
        (502, "the authentication request failed", True),
    )
    code = exc.code if exc.code in AUTH_ERROR_RESPONSES else "AUTH_SERVICE_UNAVAILABLE"
    write_error(handler, code, message, status, retryable=retryable)
