"""Read-only Dashboard projections over local strategy and catalog state."""

from __future__ import annotations

import json
import logging
from pathlib import Path
import re
from typing import Any, Callable


JsonReader = Callable[[Path, Any], Any]


def _safe_manifest_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value.encode("utf-8")) > 64 * 1024:
        return None
    if any(ord(char) < 32 and char not in "\t\n\r" for char in value):
        return None
    return value


def strategy_content(
    package: Path,
    internal_name: str,
    locale: str,
    read_json: JsonReader,
) -> dict[str, Any]:
    manifest = read_json(package / "marketplace.json", {})
    if not isinstance(manifest, dict):
        manifest = {}
    english = {
        field: _safe_manifest_text(manifest.get(field))
        for field in ("name", "summary", "description")
    }
    translated: dict[str, Any] = {}
    translations = manifest.get("translations")
    if isinstance(translations, dict):
        try:
            if len(json.dumps(translations, ensure_ascii=False).encode("utf-8")) > 64 * 1024:
                translations = None
        except (TypeError, ValueError):
            translations = None
    if locale != "en" and isinstance(translations, dict) and isinstance(translations.get(locale), dict):
        translated = translations[locale]
    fields = {
        field: _safe_manifest_text(translated.get(field)) or english[field]
        for field in ("name", "summary", "description")
    }
    translated_used = any(_safe_manifest_text(translated.get(field)) for field in fields)
    capacity = manifest.get("capacity") if isinstance(manifest.get("capacity"), dict) else {}
    markets = manifest.get("markets") if isinstance(manifest.get("markets"), dict) else {}
    assets = markets.get("assets") if isinstance(markets.get("assets"), list) else []
    risk_profile = manifest.get("risk_profile")
    return {
        "display_name": fields["name"] or internal_name,
        "summary": fields["summary"],
        "description": fields["description"],
        "content_locale": locale if translated_used else "en",
        "risk_profile": risk_profile if risk_profile in {"conservative", "balanced", "aggressive"} else None,
        "capacity_usd": capacity.get("usd") if isinstance(capacity.get("usd"), (int, float)) else None,
        "assets": [value for value in assets if isinstance(value, str) and value][:20],
    }


def strategy_records(
    root: Path,
    locale: str,
    read_json: JsonReader,
    logger: logging.Logger,
) -> list[dict[str, Any]]:
    """Describe strategy packages without exposing implementation paths."""
    if not root.is_dir():
        return []
    records = []
    for package in sorted(root.iterdir()):
        if not package.is_dir() or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,79}", package.name):
            continue
        try:
            marketplace = read_json(package / ".marketplace.json", None)
            configs = package / "configs"
            presets = sorted(path.stem for path in configs.glob("*.json")) if configs.is_dir() else []
            records.append(
                {
                    "name": package.name,
                    "presets": presets,
                    "source": "marketplace" if marketplace else "local",
                    "marketplace": marketplace,
                }
                | strategy_content(package, package.name, locale, read_json),
            )
        except OSError:
            logger.warning(
                "unreadable local strategy skipped",
                extra={
                    "event": "dashboard.strategy.invalid",
                    "result": "skipped",
                    "params": {"strategy": package.name},
                },
            )
    return records

def catalog_records(root: Path) -> list[dict[str, Any]]:
    """Return a lightweight inventory of the native parquet catalog."""
    records: list[dict[str, Any]] = []
    if not root.exists():
        return records
    for kind_dir in sorted((path for path in root.iterdir() if path.is_dir()), key=lambda path: path.name):
        for dataset in sorted((path for path in kind_dir.iterdir() if path.is_dir()), key=lambda path: path.name):
            files = list(dataset.glob("*.parquet"))
            if files:
                records.append(
                    {
                        "kind": kind_dir.name,
                        "name": dataset.name,
                        "files": len(files),
                        "bytes": sum(path.stat().st_size for path in files),
                    },
                )
    return records
