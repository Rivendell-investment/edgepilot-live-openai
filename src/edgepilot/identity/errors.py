"""Stable Live authentication failures shared across Identity capabilities."""

from __future__ import annotations


class AuthError(RuntimeError):
    def __init__(
        self,
        code: str,
        *,
        interval: int | None = None,
        status: int | None = None,
        stage: str | None = None,
        diagnostics: dict[str, int | str] | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.interval = interval
        self.status = status
        self.stage = stage
        self.diagnostics = diagnostics or {}
