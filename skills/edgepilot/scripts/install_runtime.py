#!/usr/bin/env python3
"""EdgePilot runtime installer for the published macOS and Windows targets.

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
     and require {base}/manifest.json with an immutable URL, byte size and SHA-256

The hosted custom wheel is required on every published platform.
Never fall back to PyPI nautilus_trader.

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
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

UV_VERSION = "0.11.18"
DEFAULT_RUNTIME_PYTHON = "3.12"
INTEL_MAC_UNSUPPORTED = (
    "This Mac uses an Intel processor, which EdgePilot does not support. "
    "Supported Mac computers use Apple Silicon (M-series, arm64). "
    "No runtime files were downloaded or changed."
)
ROSETTA_UNSUPPORTED = (
    "This is an Apple Silicon Mac, but the installer is running through Rosetta as x86_64. "
    "Use a native arm64 Terminal and Python, then try again. "
    "No runtime files were downloaded or changed."
)
# Cloudflare Bot Fight Mode returns 403 "error code: 1010" for Python-urllib/3.x
# on macOS, Windows, and Linux.
DOWNLOAD_USER_AGENT = (
    "Mozilla/5.0 (compatible; EdgePilot-Installer/{version}; +https://edge-pilot.rivendell.capital)"
)


def _plugin_version() -> str:
    version_path = Path(__file__).resolve().parents[3] / "src" / "edgepilot" / "VERSION"
    try:
        value = version_path.read_text(encoding="ascii")
    except OSError as exc:
        raise SystemExit(f"cannot read plugin version: {version_path}") from exc
    if value.endswith("\n"):
        value = value[:-1]
    if not re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", value):
        raise SystemExit(f"invalid plugin version: {value!r}")
    return value


DOWNLOAD_USER_AGENT = DOWNLOAD_USER_AGENT.format(version=_plugin_version())

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
    bytes: int | None = None
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


def macos_process_is_translated() -> bool:
    try:
        result = subprocess.run(
            ["/usr/sbin/sysctl", "-in", "sysctl.proc_translated"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return False
    return result.returncode == 0 and result.stdout.strip() == "1"


def platform_tuple() -> tuple[str, str]:
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "linux":
        arch = "x86_64" if machine in {"x86_64", "amd64"} else machine
        return "linux", arch
    if system == "darwin":
        if machine in {"arm64", "aarch64"}:
            return "darwin", "arm64"
        if machine in {"x86_64", "amd64"}:
            if macos_process_is_translated():
                raise SystemExit(ROSETTA_UNSUPPORTED)
            raise SystemExit(INTEL_MAC_UNSUPPORTED)
        raise SystemExit(f"unsupported platform: {system}/{machine}")
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


def wheel_python_version(wheel: WheelSelection | None) -> str | None:
    if wheel is None:
        return None
    tag = wheel.python_tag or (python_tag_from_wheel_name(wheel.filename) if wheel.filename else None)
    if not tag:
        return None
    return python_version_from_tag(tag)


def resolve_runtime_python(
    *,
    wheel: WheelSelection | None,
    override: str | None,
) -> str:
    wheel_version = wheel_python_version(wheel)
    if override:
        if wheel_version and python_version_tuple(override) != python_version_tuple(wheel_version):
            raise SystemExit(
                f"Python override {override} does not match hosted wheel {wheel_version}"
            )
        return override
    if wheel_version:
        return wheel_version
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
    # uv venv omits pip unless seeded; later steps call python -m pip.
    run([uv, "venv", "--relocatable", "--seed", "--python", python_version, str(venv)])
    if not py.is_file():
        raise SystemExit(f"venv python missing: {py}")
    return py


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def http_request(url: str, *, method: str = "GET") -> urllib.request.Request:
    request = urllib.request.Request(url, method=method)
    request.add_header("User-Agent", DOWNLOAD_USER_AGENT)
    return request


def http_get_json(url: str) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(http_request(url), timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SystemExit(f"failed to read runtime manifest: {exc}") from exc


def select_from_manifest(manifest: dict[str, Any], version: str, manifest_url: str) -> WheelSelection:
    os_name, arch = platform_tuple()
    if set(manifest) != {"package", "version", "wheels"} or manifest.get("package") != "nautilus_trader" or manifest.get("version") != version:
        raise SystemExit("runtime manifest package/version/schema is invalid")
    wheels = manifest.get("wheels")
    if not isinstance(wheels, list):
        raise SystemExit("runtime manifest wheels must be a list")

    matches: list[dict[str, Any]] = []
    for item in wheels:
        if not isinstance(item, dict) or set(item) != {"filename", "url", "bytes", "sha256", "python", "os", "arch"}:
            raise SystemExit("runtime manifest wheel schema is invalid")
        if item.get("os") != os_name:
            continue
        if item.get("arch") != arch:
            continue
        filename = item.get("filename")
        python_tag = item.get("python")
        if not isinstance(python_tag, str) or not python_tag:
            python_tag = python_tag_from_wheel_name(str(filename))
        if isinstance(filename, str) and filename.endswith(".whl") and version in filename:
            matches.append(item)

    if not matches:
        raise SystemExit(f"manifest.json has no wheel for this platform ({os_name}/{arch})")
    if len(matches) > 1:
        names = ", ".join(str(item.get("filename")) for item in matches)
        raise SystemExit(
            f"multiple wheels for this platform in manifest: {names}; "
            "publish one wheel per OS/arch or pass --wheel",
        )

    item = matches[0]
    filename = str(item["filename"])
    python_tag = str(item["python"]); digest = item["sha256"]; byte_count = item["bytes"]; url = item["url"]
    if not re.fullmatch(r"[0-9a-f]{64}", str(digest)) or not isinstance(byte_count, int) or byte_count <= 0 or not isinstance(url, str):
        raise SystemExit("runtime manifest wheel integrity fields are invalid")
    parsed, manifest_origin = urllib.parse.urlsplit(url), urllib.parse.urlsplit(manifest_url)
    if parsed.scheme != "https" or parsed.netloc != manifest_origin.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise SystemExit("runtime manifest wheel URL must be credential-free same-origin HTTPS")
    if urllib.parse.unquote(parsed.path.rsplit("/", 1)[-1]) != filename or f"/{digest}/" not in parsed.path:
        raise SystemExit("runtime manifest wheel URL is not content-addressed")
    return WheelSelection(
        filename=filename,
        python_tag=python_tag,
        sha256=str(digest),
        bytes=byte_count,
        url=url,
    )


def resolve_hosted_wheel(
    *,
    explicit_url: str | None,
    base_url: str | None,
    version: str,
    sha256: str | None,
) -> WheelSelection:
    if explicit_url:
        parsed = urllib.parse.urlsplit(explicit_url)
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise SystemExit("--wheel-url must be credential-free HTTPS")
        filename = explicit_url.rstrip("/").rsplit("/", 1)[-1]
        return WheelSelection(
            filename=filename,
            python_tag=python_tag_from_wheel_name(filename),
            sha256=sha256,
            bytes=None,
            url=explicit_url,
        )

    root = (base_url if base_url is not None else DEFAULT_WHEEL_BASE_URL).rstrip("/")
    if not root:
        raise SystemExit("runtime wheel base URL is required")
    manifest_url = f"{root}/manifest.json"
    selected = select_from_manifest(http_get_json(manifest_url), version, manifest_url)
    if sha256 and sha256.lower() != selected.sha256:
        raise SystemExit("--sha256 conflicts with the runtime manifest")
    return selected


def download_wheel(url: str, dest: Path, expected_sha256: str, expected_bytes: int | None) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file() and (expected_bytes is None or dest.stat().st_size == expected_bytes) and sha256_file(dest) == expected_sha256:
        print(f"using verified cached wheel {dest}", flush=True)
        return dest
    print(f"downloading {url}", flush=True)
    tmp = dest.with_suffix(dest.suffix + ".partial")
    try:
        with urllib.request.urlopen(http_request(url), timeout=300) as response, tmp.open("wb") as handle:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                handle.write(chunk)
        tmp.replace(dest)
    except urllib.error.HTTPError as exc:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        if exc.code == 403:
            raise SystemExit(
                "failed to download wheel: HTTP 403 (Cloudflare error code 1010). "
                "Retry with curl -L --fail -A \"Mozilla/5.0\" (Windows: curl.exe) "
                "then pass --wheel. Do not install nautilus_trader from PyPI."
            ) from exc
        raise SystemExit(f"failed to download wheel: {exc}") from exc
    except urllib.error.URLError as exc:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        raise SystemExit(f"failed to download wheel: {exc}") from exc
    actual = sha256_file(dest)
    if (expected_bytes is not None and dest.stat().st_size != expected_bytes) or actual.lower() != expected_sha256.lower():
        dest.unlink(missing_ok=True)
        raise SystemExit(f"runtime wheel integrity mismatch for {dest}")
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
        if selection.sha256 and sha256_file(selection.local_path).lower() != selection.sha256.lower():
            raise SystemExit("local runtime wheel SHA-256 mismatch")
        return selection.local_path
    if not selection.url:
        raise SystemExit(f"wheel selection missing download URL: {selection.filename}")
    if not selection.sha256:
        raise SystemExit("hosted runtime wheel requires SHA-256")
    return download_wheel(selection.url, cache_dir / selection.sha256 / selection.filename, selection.sha256, selection.bytes)


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
        help="Override runtime Python version; must match the hosted wheel tag",
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

    # Reject unsupported Mac hardware before resolving a hosted or explicitly
    # supplied wheel. This keeps the product boundary consistent and guarantees
    # that the failure happens before network access or runtime-state writes.
    platform_tuple()

    version = read_pinned_nautilus_version(plugin_root)
    wheel_selection: WheelSelection | None = None

    if args.wheel:
        wheel_path = Path(args.wheel).expanduser().resolve()
        wheel_selection = WheelSelection(
            filename=wheel_path.name,
            python_tag=python_tag_from_wheel_name(wheel_path.name),
            sha256=args.sha256,
            bytes=None,
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

        cache = venv.parent / "cache" / "wheels"
        wheel_path = materialize_wheel(wheel_selection, cache)
        install_wheel(venv_py, wheel_path)

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
