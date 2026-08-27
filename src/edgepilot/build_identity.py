"""Deterministic identities for immutable EdgePilot Live product bytes."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tomllib


_REPOSITORY_CORE = b'"../edgepilot-core/src/edgepilot_core" = "edgepilot_core"'
_BUNDLED_CORE = b'"core_src/edgepilot_core" = "edgepilot_core"'


def canonical_pyproject(data: bytes) -> bytes:
    """Make repository and bundled manifests identify the same product bytes."""
    return data.replace(_REPOSITORY_CORE, _BUNDLED_CORE)


def _content_roots(root: Path) -> list[tuple[str, Path]]:
    roots = [("product", root / "src" / "edgepilot")]
    bundled = root / "core_src" / "edgepilot_core"
    repository = root.parent / "edgepilot-core" / "src" / "edgepilot_core"
    roots.append(("core", bundled if bundled.is_dir() else repository))
    return roots


def plugin_content_digest(root: Path) -> str:
    """Hash executable product, shared core, UI assets, and dependency metadata."""
    root = root.resolve()
    digest = hashlib.sha256()
    candidates: list[tuple[str, Path]] = []
    manifest = root / "pyproject.toml"
    if manifest.is_file():
        candidates.append(("pyproject.toml", manifest))
    for prefix, source_root in _content_roots(root):
        if not source_root.is_dir():
            continue
        for path in sorted(source_root.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            relative = path.relative_to(source_root).as_posix()
            if path.suffix == ".py" or relative == "VERSION" or relative.startswith("ui_assets/"):
                candidates.append((f"{prefix}/{relative}", path))
    if not candidates:
        raise ValueError(f"EdgePilot product sources are missing from {root}")
    for name, path in sorted(candidates):
        data = path.read_bytes()
        if name == "pyproject.toml":
            data = canonical_pyproject(data)
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(len(data)).encode("ascii"))
        digest.update(b"\0")
        digest.update(data)
        digest.update(b"\n")
    return digest.hexdigest()


def verified_plugin_content_digest(root: Path, *, expected_version: str | None = None) -> str:
    """Return the digest only when a packaged plugin still matches BUILD.json.

    Repository checkouts and immutable background generations intentionally do
    not contain BUILD.json, so they continue to use their computed identity.
    Installed plugin archives must fail closed instead of running a cache tree
    containing bytes from more than one installation.
    """
    root = root.resolve()
    actual = plugin_content_digest(root)
    manifest = root / "BUILD.json"
    if not manifest.is_file():
        return actual
    try:
        build = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("PLUGIN_CACHE_INCOMPLETE: BUILD.json is unreadable") from exc
    if not isinstance(build, dict) or build.get("schema_version") != 1:
        raise RuntimeError("PLUGIN_CACHE_INCOMPLETE: BUILD.json is invalid")
    if build.get("build_id") != actual:
        raise RuntimeError("PLUGIN_CACHE_INCOMPLETE: installed plugin bytes do not match BUILD.json")
    if expected_version is not None and build.get("product_version") != expected_version:
        raise RuntimeError("PLUGIN_CACHE_INCOMPLETE: installed plugin version does not match BUILD.json")
    return actual


def runtime_contract_digest(root: Path, runtime_python: str, wheel_base: str) -> str:
    """Hash only inputs that require rebuilding Python/native dependencies."""
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    contract = {
        "runtime_python": runtime_python,
        "wheel_base": wheel_base,
        "requires_python": project.get("requires-python"),
        "dependencies": project.get("dependencies"),
    }
    return hashlib.sha256(json.dumps(contract, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
