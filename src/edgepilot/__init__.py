"""EdgePilot's agent-facing trading strategy workflows."""

from pathlib import Path
import re


_VERSION_PATTERN = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")


def _read_version() -> str:
    value = Path(__file__).with_name("VERSION").read_text(encoding="ascii")
    if value.endswith("\n"):
        value = value[:-1]
    if not _VERSION_PATTERN.fullmatch(value):
        raise RuntimeError(f"invalid EdgePilot version: {value!r}")
    return value


__version__ = _read_version()
