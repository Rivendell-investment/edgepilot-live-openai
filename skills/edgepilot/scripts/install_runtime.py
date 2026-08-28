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
     and require {base}/manifest.json with a wheel filename, byte size and SHA-256

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
import runpy
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
import urllib.parse
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

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
DOWNLOAD_USER_AGENT = (
    "Mozilla/5.0 (compatible; EdgePilot-Installer/{version}; +https://edge-pilot.rivendell.capital)"
)
PROGRESS_CALLBACK: Callable[[str, str, int | None, int | None], None] | None = None


def emit(stage: str, message: str, downloaded: int | None = None, total: int | None = None) -> None:
    print(message, flush=True)
    if PROGRESS_CALLBACK is not None:
        PROGRESS_CALLBACK(stage, message, downloaded, total)


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
DEFAULT_WHEEL_BASE_URL = "https://pub-159c6bd6a09646de8b4b871989755240.r2.dev/runtime/nautilus_trader/1.228.0/20260828"


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


def run(cmd: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
    print("+", " ".join(cmd), flush=True)
    result = subprocess.run(cmd, check=False, cwd=cwd, env=env, stderr=subprocess.PIPE, text=True)
    if result.stderr:
        print(result.stderr, end="", flush=True)
    if result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, cmd, stderr=result.stderr)


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


def uv_bin_dir() -> Path:
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "EdgePilot" / "bin"
    return Path.home() / ".edgepilot" / "bin"


def runtime_subprocess_environment(environment: dict[str, str] | None = None) -> dict[str, str]:
    """Prevent host source paths from shadowing packages installed in the runtime."""
    clean = dict(os.environ if environment is None else environment)
    clean.pop("PYTHONHOME", None)
    clean.pop("PYTHONPATH", None)
    clean["PYTHONNOUSERSITE"] = "1"
    return clean


def uv_environment(venv: Path) -> dict[str, str]:
    """Keep every uv-owned path inside the EdgePilot state directory."""
    state = venv.parent
    overrides = {
        "UV_CACHE_DIR": str(state / "cache" / "uv"),
        "UV_PYTHON_INSTALL_DIR": str(state / "python"),
        "UV_PYTHON_BIN_DIR": str(state / "bin"),
        "UV_TOOL_BIN_DIR": str(state / "tools"),
    }
    return {**runtime_subprocess_environment(), **overrides}


def _uv_download_target() -> str:
    system, arch = platform_tuple()
    if system == "darwin":
        return "aarch64-apple-darwin"
    if system == "linux":
        if arch == "x86_64":
            return "x86_64-unknown-linux-gnu"
        if arch in {"arm64", "aarch64"}:
            return "aarch64-unknown-linux-gnu"
        raise SystemExit(f"unsupported Linux arch for uv download: {arch}")
    if system == "windows":
        return "x86_64-pc-windows-msvc"
    raise SystemExit(f"unsupported platform for uv download: {system}/{arch}")


def _download_uv(dest: Path) -> None:
    target = _uv_download_target()
    executable = "uv.exe" if os.name == "nt" else "uv"
    extension = "zip" if os.name == "nt" else "tar.gz"
    url = f"https://github.com/astral-sh/uv/releases/download/{UV_VERSION}/uv-{target}.{extension}"
    print(f"Downloading uv {UV_VERSION} from {url}", flush=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    archive_tmp = dest.with_suffix(".download.tmp")
    binary_tmp = dest.with_suffix(".binary.tmp")
    try:
        with urllib.request.urlopen(http_request(url), timeout=300) as response, archive_tmp.open("wb") as handle:
            shutil.copyfileobj(response, handle)
        if extension == "tar.gz":
            with tarfile.open(archive_tmp) as archive:
                member = next(
                    item for item in archive.getmembers()
                    if item.name == executable or item.name.endswith(f"/{executable}")
                )
                source = archive.extractfile(member)
                if source is None:
                    raise SystemExit("uv binary not found in archive")
                with source, binary_tmp.open("wb") as handle:
                    shutil.copyfileobj(source, handle)
        else:
            with zipfile.ZipFile(archive_tmp) as archive:
                name = next(
                    item for item in archive.namelist()
                    if item == executable or item.endswith(f"/{executable}")
                )
                binary_tmp.write_bytes(archive.read(name))
        binary_tmp.chmod(0o755)
        result = subprocess.run([str(binary_tmp), "--version"], capture_output=True, text=True, check=False)
        if result.returncode != 0 or not result.stdout.strip().startswith(f"uv {UV_VERSION}"):
            raise SystemExit(f"uv binary verification failed: {result.stdout.strip() or result.stderr.strip()}")
        os.replace(binary_tmp, dest)
    except (OSError, StopIteration, tarfile.TarError, zipfile.BadZipFile) as exc:
        raise SystemExit(f"failed to install uv {UV_VERSION}: {exc}") from exc
    finally:
        archive_tmp.unlink(missing_ok=True)
        binary_tmp.unlink(missing_ok=True)


def find_uv() -> str | None:
    executable = "uv.exe" if os.name == "nt" else "uv"
    cached = uv_bin_dir() / executable
    if cached.is_file():
        return str(cached)
    environment_uv = Path(sys.executable).parent / executable
    if environment_uv.is_file():
        return str(environment_uv)
    return shutil.which("uv")


def ensure_uv() -> str:
    uv = find_uv()
    if uv:
        return uv
    destination = uv_bin_dir() / ("uv.exe" if os.name == "nt" else "uv")
    _download_uv(destination)
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


def ensure_venv(
    uv: str,
    venv: Path,
    python_version: str,
    *,
    env: dict[str, str] | None = None,
) -> Path:
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
    base_python = sys.executable if sys.version_info[:2] == required else python_version
    if base_python == python_version:
        run([uv, "python", "install", python_version], env=env)
    # Keep pip available for operator diagnostics; installs use uv so generated
    # entry points remain relocatable with the candidate environment.
    run([uv, "venv", "--relocatable", "--seed", "--python", base_python, str(venv)], env=env)
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
        if not isinstance(item, dict) or set(item) != {"filename", "bytes", "sha256", "python", "os", "arch", "live"}:
            raise SystemExit("runtime manifest wheel schema is invalid")
        if item.get("live") is not True:
            continue
        if item.get("os") != os_name:
            continue
        if item.get("arch") != arch:
            continue
        filename = item.get("filename")
        python_tag = item.get("python")
        if not isinstance(python_tag, str) or not python_tag:
            python_tag = python_tag_from_wheel_name(str(filename))
        if isinstance(filename, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]*\.whl", filename) and version in filename:
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
    python_tag = str(item["python"]); digest = item["sha256"]; byte_count = item["bytes"]
    if not re.fullmatch(r"[0-9a-f]{64}", str(digest)) or not isinstance(byte_count, int) or byte_count <= 0:
        raise SystemExit("runtime manifest wheel integrity fields are invalid")
    manifest = urllib.parse.urlsplit(manifest_url)
    if manifest.scheme != "https" or not manifest.netloc or manifest.username or manifest.password or manifest.query or manifest.fragment:
        raise SystemExit("runtime manifest URL must be credential-free HTTPS")
    url = urllib.parse.urljoin(manifest_url, urllib.parse.quote(filename, safe=""))
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
    emit("downloading", f"Downloading {url}", 0, expected_bytes)
    tmp = dest.with_suffix(dest.suffix + ".partial")
    try:
        with urllib.request.urlopen(http_request(url), timeout=300) as response, tmp.open("wb") as handle:
            downloaded = 0
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                handle.write(chunk)
                downloaded += len(chunk)
                emit("downloading", "Downloading locked EdgePilot runtime", downloaded, expected_bytes)
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


def pending_download_bytes(selection: WheelSelection, cache_dir: Path) -> int | None:
    """Return network bytes needed now, excluding local and verified cached wheels."""
    if selection.local_path is not None:
        return 0
    if selection.bytes is None or not selection.sha256:
        return None
    cached = cache_dir / selection.sha256 / selection.filename
    if cached.is_file() and cached.stat().st_size == selection.bytes and sha256_file(cached) == selection.sha256:
        return 0
    return selection.bytes


def install_wheel(uv: str, venv_py: Path, wheel: Path, *, env: dict[str, str] | None = None) -> None:
    print(f"installing nautilus_trader from {wheel}", flush=True)
    run([uv, "pip", "install", "--python", str(venv_py), "--force-reinstall", "--no-deps", str(wheel)], env=env)


def install_plugin(uv: str, venv_py: Path, plugin_root: Path, *, env: dict[str, str] | None = None) -> None:
    print(f"installing EdgePilot from {plugin_root}", flush=True)
    # The custom Nautilus wheel is already installed at the exact version pinned by
    # pyproject.toml, so dependency resolution keeps it and installs the remaining
    # Nautilus and EdgePilot dependencies into a fresh user runtime.
    bundled_core = plugin_root / "core_src" / "edgepilot_core" / "__init__.py"
    if bundled_core.is_file():
        run([uv, "pip", "install", "--python", str(venv_py), str(plugin_root)], env=env)
        return

    repository_core = plugin_root.parent / "edgepilot-core"
    repository_package = repository_core / "src" / "edgepilot_core" / "__init__.py"
    if repository_package.is_file():
        run([uv, "pip", "install", "--python", str(venv_py), str(plugin_root)], env=env)
        return

    raise SystemExit(
        "shared core missing: checked bundled "
        f"{bundled_core.parent} and repository {repository_package.parent}"
    )


def runtime_state_path(venv: Path) -> Path:
    return venv.parent / "runtime.json"


def runtime_contract_digest(plugin_root: Path, runtime_python: str) -> str:
    """Identify native/runtime dependencies without binding ordinary plugin code."""
    module = runpy.run_path(
        str(plugin_root / "src" / "edgepilot" / "service" / "build_identity.py"),
    )
    function = module.get("runtime_contract_digest")
    if not callable(function):
        raise SystemExit("EdgePilot runtime contract helper is unavailable")
    return str(function(plugin_root, runtime_python, DEFAULT_WHEEL_BASE_URL))


def reusable_native_runtime(
    venv: Path,
    state: object,
    *,
    plugin_root: Path,
    wheel: WheelSelection,
    runtime_python: str,
) -> bool:
    if not isinstance(state, dict) or state.get("schema_version") != 3:
        return False
    python = venv_python(venv)
    if not python.is_file() or read_python_version(python) != python_version_tuple(runtime_python):
        return False
    contract = runtime_contract_digest(plugin_root, runtime_python)
    return (
        state.get("runtime_contract_digest") == contract
        and isinstance(wheel.sha256, str)
        and state.get("wheel_sha256") == wheel.sha256
        and isinstance(state.get("native_runtime_fingerprint"), str)
        and len(state["native_runtime_fingerprint"]) == 64
    )


def plugin_content_digest(plugin_root: Path) -> str:
    module = runpy.run_path(
        str(plugin_root / "src" / "edgepilot" / "service" / "build_identity.py"),
    )
    function = module.get("plugin_content_digest")
    if not callable(function):
        raise SystemExit("EdgePilot build identity helper is unavailable")
    return str(function(plugin_root))


def runtime_metadata(
    plugin_root: Path,
    *,
    nautilus_version: str,
    wheel: WheelSelection,
    wheel_path: Path,
    runtime_python: str,
) -> dict[str, Any]:
    content_digest = plugin_content_digest(plugin_root)
    wheel_digest = sha256_file(wheel_path)
    contract_digest = runtime_contract_digest(plugin_root, runtime_python)
    native_fingerprint = hashlib.sha256(
        f"{contract_digest}\0{nautilus_version}\0{wheel.filename}\0{wheel_digest}\0{runtime_python}".encode()
    ).hexdigest()
    return {
        "plugin_version": (plugin_root / "src" / "edgepilot" / "VERSION").read_text(encoding="ascii").strip(),
        "plugin_content_digest": content_digest,
        "nautilus_version": nautilus_version,
        "wheel_filename": wheel.filename,
        "wheel_sha256": wheel_digest,
        "runtime_python": runtime_python,
        "runtime_contract_digest": contract_digest,
        "native_runtime_fingerprint": native_fingerprint,
        "runtime_fingerprint": native_fingerprint,
    }


def write_runtime_state(venv: Path, previous: Path | None, metadata: dict[str, Any]) -> None:
    path = runtime_state_path(venv)
    value = {
        "schema_version": 3,
        "active_release": venv.name,
        "previous_release": previous.name if previous is not None and previous.exists() else None,
        "activated_at": int(time.time()),
        **metadata,
    }
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    temporary.replace(path)


def activate_candidate(candidate: Path, active: Path, metadata: dict[str, Any] | None = None) -> Path | None:
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
        write_runtime_state(active, previous if moved_previous else None, metadata or {
            "plugin_version": "unknown",
            "plugin_content_digest": "",
            "nautilus_version": "unknown",
            "wheel_filename": "unknown",
            "wheel_sha256": "",
            "runtime_python": "unknown",
            "runtime_contract_digest": "",
            "native_runtime_fingerprint": "",
            "runtime_fingerprint": "",
        })
        return previous if moved_previous else None
    except BaseException:
        if active.exists():
            shutil.rmtree(active)
        if moved_previous and previous.exists():
            previous.rename(active)
        raise


def verify(venv_py: Path, venv: Path) -> None:
    code = (
        "import edgepilot, edgepilot_core, importlib.metadata, nautilus_trader, pathlib, sys; "
        "import nautilus_trader.adapters.bitget, nautilus_trader.adapters.gateio; "
        "edgepilot_path=pathlib.Path(edgepilot.__file__).resolve(); "
        "runtime_prefix=pathlib.Path(sys.prefix).resolve(); "
        "installed_version=importlib.metadata.version('edgepilot'); "
        "print(f'edgepilot={edgepilot_path}'); "
        "print(f'runtime_prefix={runtime_prefix}'); "
        "print(f'edgepilot_version={edgepilot.__version__} installed_version={installed_version}'); "
        "print(f'nautilus_trader={nautilus_trader.__file__}'); "
        "print(f'nautilus_version={getattr(nautilus_trader, \"__version__\", \"?\")}'); "
        "assert edgepilot_path.is_relative_to(runtime_prefix), "
        "f'EdgePilot imported outside runtime: {edgepilot_path} not under {runtime_prefix}'; "
        "assert installed_version == edgepilot.__version__, "
        "f'EdgePilot version mismatch: metadata={installed_version} import={edgepilot.__version__}'"
    )
    environment = runtime_subprocess_environment()
    run([str(venv_py), "-c", code], env=environment)
    entry = venv_edgepilot(venv)
    if not entry.is_file():
        raise SystemExit(f"edgepilot entry point missing: {entry}")
    run([str(entry), "--help"], env=environment)
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
    emit("preparing", "Checking platform, runtime version, and installation path")
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
    uv = ensure_uv()
    venv = (args.venv or default_venv_dir()).expanduser().resolve()
    uv_env = uv_environment(venv)
    cache = venv.parent / "cache" / "wheels"
    download_total = pending_download_bytes(wheel_selection, cache)
    emit("preparing", f"Runtime Python: {runtime_python}", 0, download_total)
    candidate = venv.with_name(f".{venv.name}.candidate-{os.getpid()}")
    if candidate.exists():
        shutil.rmtree(candidate)
    try:
        try:
            previous_state: object = json.loads(runtime_state_path(venv).read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            previous_state = None
        cached_wheel = cache / str(wheel_selection.sha256) / wheel_selection.filename
        reuse_native = cached_wheel.is_file() and reusable_native_runtime(
            venv,
            previous_state,
            plugin_root=plugin_root,
            wheel=wheel_selection,
            runtime_python=runtime_python,
        )
        if reuse_native:
            emit("installing", "Reusing the existing Python and native runtime", 0, 0)
            shutil.copytree(venv, candidate, symlinks=True)
            venv_py = venv_python(candidate)
            wheel_path = cached_wheel
            downloaded = 0
        else:
            emit("installing", "Creating the isolated EdgePilot runtime", 0, download_total)
            venv_py = ensure_venv(uv, candidate, runtime_python, env=uv_env)
            wheel_path = materialize_wheel(wheel_selection, cache)
            downloaded = download_total if download_total is not None else None
            emit("verifying", "Verifying the runtime wheel", downloaded, download_total)
            install_wheel(uv, venv_py, wheel_path, env=uv_env)

        emit("installing", "Installing EdgePilot and locked runtime dependencies", downloaded, download_total)
        install_plugin(uv, venv_py, plugin_root, env=uv_env)
        run([uv, "pip", "check", "--python", str(venv_py)], env=uv_env)
        verify(venv_py, candidate)
        metadata = runtime_metadata(
            plugin_root,
            nautilus_version=version,
            wheel=wheel_selection,
            wheel_path=wheel_path,
            runtime_python=runtime_python,
        )
        activate_candidate(candidate, venv, metadata)
        emit("ready", "EdgePilot runtime is ready", downloaded, download_total)
    finally:
        if candidate.exists():
            shutil.rmtree(candidate)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as exc:
        raise SystemExit(exc.returncode) from exc
