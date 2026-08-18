"""Shared locale normalization for EdgePilot user interfaces."""

from __future__ import annotations

import re
from typing import Final


SUPPORTED_LANGUAGES: Final[tuple[str, ...]] = ("en", "ko", "zh-CN", "zh-TW")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_TAG = re.compile(r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*$")


def _well_formed_bcp47(parts: list[str]) -> bool:
    """Validate the BCP 47 structure needed before applying product mappings."""
    language = parts[0]
    index = 1
    extlangs = 0
    while index < len(parts) and len(language) <= 3 and len(parts[index]) == 3 and parts[index].isalpha() and extlangs < 3:
        index += 1
        extlangs += 1
    if index < len(parts) and len(parts[index]) == 4 and parts[index].isalpha():
        index += 1
    if index < len(parts) and ((len(parts[index]) == 2 and parts[index].isalpha()) or (len(parts[index]) == 3 and parts[index].isdigit())):
        index += 1
    while index < len(parts) and ((5 <= len(parts[index]) <= 8) or (len(parts[index]) == 4 and parts[index][0].isdigit())):
        index += 1
    singletons: set[str] = set()
    while index < len(parts) and len(parts[index]) == 1 and parts[index].lower() != "x":
        singleton = parts[index].lower()
        if singleton in singletons:
            return False
        singletons.add(singleton)
        index += 1
        start = index
        while index < len(parts) and 2 <= len(parts[index]) <= 8:
            index += 1
        if index == start:
            return False
    if index < len(parts) and parts[index].lower() == "x":
        index += 1
        if index == len(parts):
            return False
        return all(1 <= len(part) <= 8 for part in parts[index:])
    return index == len(parts)


def normalize_supported_locale(value: object) -> str | None:
    """Return EdgePilot's canonical locale or ``None`` for an invalid candidate."""
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate or len(candidate) > 64 or _CONTROL.search(candidate):
        return None
    candidate = candidate.replace("_", "-")
    if not _TAG.fullmatch(candidate):
        return None
    parts = candidate.split("-")
    if not _well_formed_bcp47(parts):
        return None
    language = parts[0].lower()
    if language == "en":
        return "en"
    if language == "ko":
        return "ko"
    if language != "zh":
        return None
    script = next((part.title() for part in parts[1:] if len(part) == 4 and part.isalpha()), None)
    region = next((part.upper() for part in parts[1:] if len(part) == 2 and part.isalpha()), None)
    if script == "Hant" or region in {"TW", "HK", "MO"}:
        return "zh-TW"
    if script == "Hans" or region in {"CN", "SG"}:
        return "zh-CN"
    return "zh-CN"
