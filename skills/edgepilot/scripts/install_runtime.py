#!/usr/bin/env python3
"""Cross-platform EdgePilot runtime installer (macOS / Windows / Linux).

This script does NOT vendor or compile NautilusTrader. Internal developers build
wheels separately and upload them to the marketplace runtime host. Users (via
the Skill) create a local venv with the Python version required by the hosted
wheel (via uv), download/install that prebuilt wheel, then install this plugin.

Usage:
  python3 install_runtime.py <plugin-root>

Wheel resolution (first match wins):
  1) --wheel / EDGEPILOT_NAUTILUS_WHEEL
  2) --wheel-url / EDGEPILOT_NAUTILUS_WHEEL_URL
  3) --wheel-base-url / EDGEPILOT_NAUTILUS_WHEEL_BASE_URL / DEFAULT_WHEEL_BASE_URL
     then prefer {base}/manifest.json, else try platform filename candidates
  4) otherwise let pyproject pull nautilus_trader from PyPI (transition only)

Optional env:
  EDGEPILOT_VENV
  EDGEPILOT_PYTHON
  EDGEPILOT_NAUTILUS_SHA256
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

UV_VERSION = "0.11.18"
DEFAULT_RUNTIME_PYTHON = "3.12"

# Default marketplace runtime host. Expected layout:
#   {base}/manifest.json
#   {base}/nautilus_trader-<ver>-<tag>.whl
#   {base}/SHA256SUMS
DEFAULT_WHEEL_BASE_URL = "https://edge-pilot.rivendell.capital/runtime/nautilus_trader/1.228.0"


@dataclass(frozen=True)
class WheelSelection:
    filename: str
    python_tag: str | None
    sha256: str | None
    local_path: Path | None = None
    url: str | None = None


def default_venv_dir() -> Path:
    override = os.environ.get("EDGEPILOT_VENV")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "EdgePilot" / ".venv"
    return Path.home() / ".edgepilot" / ".venv"


def venv_python(venv: Path) -> Path:
    if os.name == "nt":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def venv_edgepilot(venv: Path) -> Path:
    if os.name == "nt":
        return venv / "Scripts" / "edgepilot.exe"
    return venv / "bin" / "edgepilot"


def run(cmd: list[str], *, cwd: Path | None = None) -> None:
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, cwd=cwd)


def platform_tuple() -> tuple[str, str]:
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "linux":
        arch = "x86_64" if machine in {"x86_64", "amd64"} else machine
        return "linux", arch
    if system == "darwin":
        arch = "arm64" if machine in {"arm64", "aarch64"} else "x86_64"
        return "darwin", arch
    if system == "windows":
        arch = "amd64" if machine in {"amd64", "x86_64"} else machine
        return "windows", arch
    raise SystemExit(f"unsupported platform: {system}/{machine}")


def python_tag_from_wheel_name(name: str) -> str | None:
    for part in Path(name).name.split("-"):
        if re.fullmatch(r"cp\d+", part):
            return part
    return None


def python_version_from_tag(tag: str) -> str:
    if not tag.startswith("cp") or len(tag) < 4 or not tag[2:].isdigit():
        raise SystemExit(f"unsupported python wheel tag: {tag}")
    digits = tag[2:]
    if not digits.startswith("3") or len(digits) < 2:
        raise SystemExit(f"unsupported python wheel tag: {tag}")
    return f"{digits[0]}.{digits[1:]}"


def python_version_tuple(version: str) -> tuple[int, int]:
    major, minor = version.split(".", 1)
    return int(major), int(minor)


def read_python_version(python_bin: Path | str) -> tuple[int, int]:
    result = subprocess.run(
        [str(python_bin), "-c", "import sys; print(sys.version_info[0], sys.version_info[1])"],
        check=True,
        capture_output=True,
        text=True,
    )
    major, minor = result.stdout.strip().split()
    return int(major), int(minor)


def find_uv() -> str | None:
    return shutil.which("uv")


def ensure_uv() -> str:
    uv = find_uv()
    if uv:
        return uv
    print(f"uv not found; installing uv=={UV_VERSION} via pip", flush=True)
    run([sys.executable, "-m", "pip", "install", f"uv=={UV_VERSION}"])
    uv = find_uv()
    if not uv:
        raise SystemExit("uv installation failed; install uv manually")
    return uv


def resolve_runtime_python(
    *,
    wheel: WheelSelection | None,
    override: str | None,
) -> str:
    if override and python_version_tuple(override) != (3, 12):
        raise SystemExit(f"EdgePilot Live requires Python 3.12, found override {override}")
    if override:
        return override
    if wheel and wheel.python_tag:
        version = python_version_from_tag(wheel.python_tag)
        if python_version_tuple(version) != (3, 12):
            raise SystemExit(f"EdgePilot Live requires a CPython 3.12 wheel, found {wheel.python_tag}")
        return version
    if wheel and wheel.filename:
        tag = python_tag_from_wheel_name(wheel.filename)
        if tag:
            version = python_version_from_tag(tag)
            if python_version_tuple(version) != (3, 12):
                raise SystemExit(f"EdgePilot Live requires a CPython 3.12 wheel, found {tag}")
            return version
    return DEFAULT_RUNTIME_PYTHON


def ensure_venv(uv: str, venv: Path, python_version: str) -> Path:
    py = venv_python(venv)
    required = python_version_tuple(python_version)
    if py.is_file():
        existing = read_python_version(py)
        if existing != required:
            raise SystemExit(
                f"existing venv uses Python {existing[0]}.{existing[1]} "
                f"but runtime requires {python_version}; "
                f"remove {venv} or pass --venv with a fresh path",
            )
        return py

    venv.parent.mkdir(parents=True, exist_ok=True)
    run([uv, "python", "install", python_version])
    run([uv, "venv", "--relocatable", "--python", python_version, str(venv)])
    if not py.is_file():
        raise SystemExit(f"venv python missing: {py}")
    return py


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def candidate_filenames(version: str, python_tag: str | None = None) -> list[str]:
    os_name, arch = platform_tuple()
    py = python_tag or "cp312"
    if os_name == "linux":
        tags = [
            f"{py}-{py}-manylinux_2_43_{arch}",
            f"{py}-{py}-manylinux_2_35_{arch}",
            f"{py}-{py}-manylinux2014_{arch}",
            f"{py}-{py}-linux_{arch}",
        ]
    elif os_name == "darwin":
        tags = [
            f"{py}-{py}-macosx_15_0_{arch}",
            f"{py}-{py}-macosx_14_0_{arch}",
            f"{py}-{py}-macosx_11_0_{arch}",
        ]
    else:
        tags = [
            f"{py}-{py}-win_{arch}",
            f"{py}-{py}-win_amd64" if arch == "amd64" else f"{py}-{py}-win_{arch}",
        ]
    return [f"nautilus_trader-{version}-{tag}.whl" for tag in tags]


def http_get_json(url: str) -> dict[str, Any] | None:
    try:
        with urllib.request.urlopen(url) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def http_exists(url: str) -> bool:
    request = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(request) as response:
            return 200 <= getattr(response, "status", 200) < 300
    except urllib.error.HTTPError as exc:
        if exc.code in {403, 405}:
            return True
        return False
    except (urllib.error.URLError, TimeoutError):
        return False


def select_from_manifest(manifest: dict[str, Any], version: str) -> WheelSelection | None:
    os_name, arch = platform_tuple()
    wheels = manifest.get("wheels")
    if not isinstance(wheels, list):
        return None

    matches: list[dict[str, Any]] = []
    for item in wheels:
        if not isinstance(item, dict):
            continue
        if item.get("os") not in {None, os_name}:
            continue
        if item.get("arch") not in {None, arch}:
            continue
        filename = item.get("filename")
        python_tag = item.get("python")
        if not isinstance(python_tag, str) or not python_tag:
            python_tag = python_tag_from_wheel_name(str(filename))
        if python_tag != "cp312":
            continue
        if isinstance(filename, str) and filename.endswith(".whl") and version in filename:
            matches.append(item)

    if not matches:
        return None
    if len(matches) > 1:
        names = ", ".join(str(item.get("filename")) for item in matches)
        raise SystemExit(
            f"multiple wheels for this platform in manifest: {names}; "
            "publish one wheel per OS/arch or pass --wheel",
        )

    item = matches[0]
    filename = str(item["filename"])
    python_tag = item.get("python")
    if not isinstance(python_tag, str) or not python_tag:
        python_tag = python_tag_from_wheel_name(filename)
    return WheelSelection(
        filename=filename,
        python_tag=python_tag,
        sha256=str(item["sha256"]) if item.get("sha256") else None,
    )


def resolve_hosted_wheel(
    *,
    explicit_url: str | None,
    base_url: str | None,
    version: str,
    sha256: str | None,
) -> WheelSelection | None:
    if explicit_url:
        filename = explicit_url.rstrip("/").rsplit("/", 1)[-1]
        return WheelSelection(
            filename=filename,
            python_tag=python_tag_from_wheel_name(filename),
            sha256=sha256,
            url=explicit_url,
        )

    root = (base_url if base_url is not None else DEFAULT_WHEEL_BASE_URL).rstrip("/")
    if not root:
        return None

    manifest = http_get_json(f"{root}/manifest.json")
    if manifest:
        selected = select_from_manifest(manifest, version)
        if selected:
            return WheelSelection(
                filename=selected.filename,
                python_tag=selected.python_tag,
                sha256=sha256 or selected.sha256,
                url=f"{root}/{selected.filename}",
            )
        os_name, _ = platform_tuple()
        raise SystemExit(
            f"manifest.json has no wheel for this platform ({os_name}); "
            "provide --wheel/--wheel-url",
        )

    for filename in candidate_filenames(version):
        url = f"{root}/{filename}"
        if http_exists(url):
            return WheelSelection(
                filename=filename,
                python_tag=python_tag_from_wheel_name(filename),
                sha256=sha256,
                url=url,
            )
    tried = ", ".join(candidate_filenames(version))
    raise SystemExit(f"no wheel found under {root}; tried: {tried}")


def download_wheel(url: str, dest: Path, expected_sha256: str | None) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"downloading {url}", flush=True)
    tmp = dest.with_suffix(dest.suffix + ".partial")
    try:
        with urllib.request.urlopen(url) as response, tmp.open("wb") as handle:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                handle.write(chunk)
        tmp.replace(dest)
    except urllib.error.URLError as exc:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        raise SystemExit(f"failed to download wheel: {exc}") from exc
    if expected_sha256:
        actual = sha256_file(dest)
        if actual.lower() != expected_sha256.lower():
            dest.unlink(missing_ok=True)
            raise SystemExit(
                f"sha256 mismatch for {dest}\nexpected {expected_sha256}\nactual   {actual}",
            )
    return dest


def read_pinned_nautilus_version(plugin_root: Path) -> str:
    text = (plugin_root / "pyproject.toml").read_text(encoding="utf-8")
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("\"nautilus_trader==") or stripped.startswith("'nautilus_trader=="):
            return stripped.split("==", 1)[1].rstrip("\",'")
    raise SystemExit("could not find nautilus_trader==… pin in pyproject.toml")


def materialize_wheel(selection: WheelSelection, cache_dir: Path) -> Path:
    if selection.local_path is not None:
        if not selection.local_path.is_file():
            raise SystemExit(f"wheel not found: {selection.local_path}")
        return selection.local_path
    if not selection.url:
        raise SystemExit(f"wheel selection missing download URL: {selection.filename}")
    return download_wheel(selection.url, cache_dir / selection.filename, selection.sha256)


def install_wheel(venv_py: Path, wheel: Path) -> None:
    print(f"installing nautilus_trader from {wheel}", flush=True)
    run([str(venv_py), "-m", "pip", "install", "--force-reinstall", "--no-deps", str(wheel)])


def install_plugin(venv_py: Path, plugin_root: Path) -> None:
    print(f"installing EdgePilot from {plugin_root}", flush=True)
    core = plugin_root / "backtest_core_src"
    if not (core / "edgepilot_backtest_core" / "__init__.py").is_file():
        raise SystemExit(f"bundled backtest core missing: {core}")
    # The custom Nautilus wheel is already installed at the exact version pinned by
    # pyproject.toml, so dependency resolution keeps it and installs the remaining
    # Nautilus and EdgePilot dependencies into a fresh user runtime.
    run([str(venv_py), "-m", "pip", "install", "-e", str(plugin_root)])


def runtime_state_path(venv: Path) -> Path:
    return venv.parent / "runtime.json"


def write_runtime_state(venv: Path, previous: Path | None) -> None:
    path = runtime_state_path(venv)
    value = {
        "schema_version": 1,
        "active_release": venv.name,
        "previous_release": previous.name if previous is not None and previous.exists() else None,
        "activated_at": int(time.time()),
    }
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    temporary.replace(path)


def activate_candidate(candidate: Path, active: Path) -> Path | None:
    """Activate a verified relocatable venv and restore the old one on failure."""
    previous = active.with_name(f"{active.name}.previous")
    if previous.exists():
        shutil.rmtree(previous)
    moved_previous = False
    try:
        if active.exists():
            active.rename(previous)
            moved_previous = True
        candidate.rename(active)
        verify(venv_python(active), active)
        write_runtime_state(active, previous if moved_previous else None)
        return previous if moved_previous else None
    except BaseException:
        if active.exists():
            shutil.rmtree(active)
        if moved_previous and previous.exists():
            previous.rename(active)
        raise


def verify(venv_py: Path, venv: Path) -> None:
    code = (
        "import edgepilot, edgepilot_backtest_core, nautilus_trader; "
        "import nautilus_trader.adapters.bitget, nautilus_trader.adapters.gateio; "
        "print(f'edgepilot={edgepilot.__file__}'); "
        "print(f'nautilus_trader={nautilus_trader.__file__}'); "
        "print(f'nautilus_version={getattr(nautilus_trader, \"__version__\", \"?\")}')"
    )
    run([str(venv_py), "-c", code])
    entry = venv_edgepilot(venv)
    if not entry.is_file():
        raise SystemExit(f"edgepilot entry point missing: {entry}")
    run([str(entry), "--help"])
    print(f"ok: {entry}", flush=True)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plugin_root", type=Path, help="Directory containing edgepilot/pyproject.toml")
    parser.add_argument("--venv", type=Path, default=None, help="Override venv path")
    parser.add_argument(
        "--python",
        default=os.environ.get("EDGEPILOT_PYTHON"),
        help="Override runtime Python version for the venv (e.g. 3.12)",
    )
    parser.add_argument("--wheel", default=os.environ.get("EDGEPILOT_NAUTILUS_WHEEL"))
    parser.add_argument("--wheel-url", default=os.environ.get("EDGEPILOT_NAUTILUS_WHEEL_URL"))
    parser.add_argument(
        "--wheel-base-url",
        default=os.environ.get("EDGEPILOT_NAUTILUS_WHEEL_BASE_URL"),
        help="Directory URL that contains manifest.json and nautilus_trader wheels",
    )
    parser.add_argument("--sha256", default=os.environ.get("EDGEPILOT_NAUTILUS_SHA256"))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    plugin_root = args.plugin_root.expanduser().resolve()
    if not (plugin_root / "pyproject.toml").is_file():
        raise SystemExit(f"plugin root missing pyproject.toml: {plugin_root}")

    version = read_pinned_nautilus_version(plugin_root)
    wheel_selection: WheelSelection | None = None

    if args.wheel:
        wheel_path = Path(args.wheel).expanduser().resolve()
        wheel_selection = WheelSelection(
            filename=wheel_path.name,
            python_tag=python_tag_from_wheel_name(wheel_path.name),
            sha256=args.sha256,
            local_path=wheel_path,
        )
    else:
        wheel_selection = resolve_hosted_wheel(
            explicit_url=args.wheel_url,
            base_url=args.wheel_base_url,
            version=version,
            sha256=args.sha256,
        )

    runtime_python = resolve_runtime_python(wheel=wheel_selection, override=args.python)
    print(f"runtime python: {runtime_python}", flush=True)

    uv = ensure_uv()
    venv = (args.venv or default_venv_dir()).expanduser().resolve()
    candidate = venv.with_name(f".{venv.name}.candidate-{os.getpid()}")
    if candidate.exists():
        shutil.rmtree(candidate)
    try:
        venv_py = ensure_venv(uv, candidate, runtime_python)
        run([str(venv_py), "-m", "pip", "install", "--upgrade", "pip"])

        if wheel_selection is not None:
            cache = venv.parent / "cache" / "wheels"
            wheel_path = materialize_wheel(wheel_selection, cache)
            install_wheel(venv_py, wheel_path)
        else:
            print(
                "warning: no prebuilt wheel configured; "
                "pyproject may install nautilus_trader from PyPI",
                file=sys.stderr,
                flush=True,
            )

        install_plugin(venv_py, plugin_root)
        run([str(venv_py), "-m", "pip", "check"])
        verify(venv_py, candidate)
        activate_candidate(candidate, venv)
    finally:
        if candidate.exists():
            shutil.rmtree(candidate)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as exc:
        raise SystemExit(exc.returncode) from exc
