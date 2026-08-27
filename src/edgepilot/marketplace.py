"""Public marketplace client and safe strategy package installer."""

from __future__ import annotations

import io
from datetime import datetime, timezone
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path
from contextlib import contextmanager
from typing import Any
from urllib.parse import quote, urlencode
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from edgepilot import auth
from edgepilot.file_lock import FileLock
from edgepilot.marketplace_origin import marketplace_origin
from edgepilot.paths import strategies_state_root


def strategies_root() -> Path:
    root = strategies_state_root()
    root.mkdir(parents=True, exist_ok=True)
    (root / "__init__.py").touch(exist_ok=True)
    return root


RISK_PROFILE_ALIASES = {
    "conservative": "conservative",
    "balanced": "balanced",
    "aggressive": "aggressive",
    "稳健": "conservative",
    "平衡": "balanced",
    "激进": "aggressive",
}
MAX_ARCHIVE_BYTES = 20 * 1024 * 1024
MAX_EXPANDED_BYTES = 50 * 1024 * 1024
MAX_ERROR_BYTES = 16 * 1024
MAX_RETRY_AFTER = 2_678_400

_RETRYABLE_MARKETPLACE_CODES = {
    "CATALOG_COVERAGE_INSUFFICIENT", "RATE_LIMITED", "SERVICE_UNAVAILABLE", "AUTH_SERVICE_UNAVAILABLE",
}


class MarketplaceRequestError(RuntimeError):
    """A controlled remote error exposed only by opted-in Marketplace calls."""

    def __init__(self, code: str, status: int | None, retryable: bool, *,
                 retry_after: int | None = None, limiting_scopes: list[str] | None = None,
                 quotas: dict[str, dict[str, dict[str, Any]]] | None = None):
        super().__init__(code)
        self.code = code
        self.status = status
        self.retryable = retryable
        self.retry_after = retry_after
        self.limiting_scopes = list(limiting_scopes or [])
        self.quotas = dict(quotas or {})

    def public_details(self) -> dict[str, Any]:
        details: dict[str, Any] = {"retryable": self.retryable}
        if self.retry_after is not None:
            details["retry_after"] = self.retry_after
        if self.limiting_scopes:
            details["limiting_scopes"] = self.limiting_scopes
        if self.quotas:
            details["quotas"] = self.quotas
        return details


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


_SAFE_OPENER = build_opener(_NoRedirect)


def urlopen(request: Request, *, timeout: int):  # test seam; never forwards Bearer credentials to redirects
    return _SAFE_OPENER.open(request, timeout=timeout)


@contextmanager
def strategy_operation_lock(name: str):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", name):
        raise ValueError("invalid strategy lock name")
    root = strategies_root()
    locks = root / ".locks"
    locks.mkdir(parents=True, exist_ok=True)
    # Marketplace slugs use hyphens while local discovery exposes the same
    # strategy with underscores. Canonicalize first so install/update/remove
    # and trading startup serialize on one opaque lock file.
    canonical = name.replace("-", "_").lower()
    lock_id = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    with FileLock(str(locks / f"strategy-{lock_id}.lock")):
        yield


def _safe_directory(root: Path, name: str) -> Path:
    path = (root / name).resolve()
    if root.resolve() not in path.parents:
        raise ValueError("invalid marketplace path")
    return path


def preflight_install(name: str, version: str) -> None:
    """Reject deterministic local conflicts before login or network access."""
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,79}", name) or not re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z.+-]{0,63}", version):
        raise ValueError("invalid marketplace strategy name or version")
    destination = _safe_directory(strategies_root(), name.replace("-", "_"))
    if not destination.exists():
        return
    try:
        previous = json.loads((destination / ".marketplace.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("a local strategy already uses this name; rename or remove it before installing") from exc
    if previous.get("slug") != name:
        raise ValueError("a different local strategy already uses this name")
    if previous.get("version") == version:
        raise ValueError("this Marketplace version is already installed")
    from edgepilot.run_state import running_runs
    active = running_runs(destination / "runs") if (destination / "runs").exists() else {}
    if active:
        raise ValueError("stop active runs before updating this strategy")


def install_package(name: str, version: str, archive: bytes) -> dict[str, str]:
    with strategy_operation_lock(name):
        return _install_package_unlocked(name, version, archive)


def _install_package_unlocked(name: str, version: str, archive: bytes) -> dict[str, str]:
    """Safely install or update one marketplace package in persistent local state."""
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,79}", name):
        raise ValueError("invalid marketplace strategy name")
    if not re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z.+-]{0,63}", version):
        raise ValueError("invalid marketplace strategy version")
    if not archive or len(archive) > MAX_ARCHIVE_BYTES:
        raise ValueError("marketplace package must be between 1 byte and 20 MiB")
    local_name = name.replace("-", "_")
    root = strategies_root()
    destination = _safe_directory(root, local_name)
    previous: dict[str, Any] | None = None
    if destination.exists():
        previous_path = destination / ".marketplace.json"
        try:
            previous = json.loads(previous_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raise ValueError("a local strategy already uses this name; rename or remove it before installing")
        if previous.get("slug") != name:
            raise ValueError("a different local strategy already uses this name")
        if previous.get("version") == version:
            raise ValueError("this Marketplace version is already installed")
        from edgepilot.run_state import running_runs
        active = running_runs(destination / "runs") if (destination / "runs").exists() else {}
        if active:
            raise ValueError("stop active runs before updating this strategy")
    try:
        package = zipfile.ZipFile(io.BytesIO(archive))
    except zipfile.BadZipFile as exc:
        raise ValueError("marketplace package is not a valid ZIP file") from exc
    with package:
        entries = [entry for entry in package.infolist() if not entry.is_dir()]
        if not entries or len(entries) > 500 or sum(entry.file_size for entry in entries) > MAX_EXPANDED_BYTES:
            raise ValueError("marketplace package is outside supported size limits")
        paths: list[Path] = []
        for entry in entries:
            path = Path(entry.filename)
            if path.is_absolute() or ".." in path.parts or not path.parts or stat.S_ISLNK(entry.external_attr >> 16):
                raise ValueError("marketplace package contains an unsafe path")
            paths.append(path)
        roots = {path.parts[0] for path in paths}
        prefix = next(iter(roots)) if len(roots) == 1 and Path(next(iter(roots)), "strategy.py") in paths else None
        relative_paths = [path.relative_to(prefix) if prefix else path for path in paths]
        if Path("strategy.py") not in relative_paths or Path("__init__.py") not in relative_paths:
            raise ValueError("marketplace package must contain one native strategy package")
        temporary = _safe_directory(root, f".{local_name}.installing")
        if temporary.exists():
            shutil.rmtree(temporary)
        temporary.mkdir(parents=True)
        try:
            # The lightweight localhost MCP process can run on macOS' bundled
            # Python 3.9 before the managed runtime is installed. ``strict``
            # was only added to zip() in Python 3.10.
            for entry, relative in zip(entries, relative_paths):
                target = (temporary / relative).resolve()
                if temporary.resolve() not in target.parents:
                    raise ValueError("marketplace package contains an unsafe path")
                target.parent.mkdir(parents=True, exist_ok=True)
                with package.open(entry) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
            (temporary / ".marketplace.json").write_text(
                json.dumps({"source": "edgepilot-marketplace", "slug": name, "version": version}, indent=2) + "\n",
                encoding="utf-8",
            )
            if previous is None:
                temporary.replace(destination)
            else:
                # The package default is owned by its published version. Preserve
                # user-created named configurations and all local runs, but never
                # let an older ``configs/default.json`` mask new package defaults.
                configs = destination / "configs"
                if configs.exists():
                    for source in configs.iterdir():
                        if source.name == "default.json":
                            continue
                        target = temporary / "configs" / source.name
                        if source.is_dir():
                            shutil.copytree(source, target, dirs_exist_ok=True)
                        else:
                            target.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(source, target)
                runs = destination / "runs"
                if runs.exists():
                    shutil.copytree(runs, temporary / "runs", dirs_exist_ok=True)
                replaced = _safe_directory(root, f".{local_name}.replaced")
                if replaced.exists():
                    shutil.rmtree(replaced)
                destination.replace(replaced)
                try:
                    temporary.replace(destination)
                except Exception:
                    replaced.replace(destination)
                    raise
                shutil.rmtree(replaced)
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
    return {"installed": local_name, "version": version, "action": "updated" if previous else "installed"}


def _request(path: str, query: dict[str, str] | None = None, *, method: str = "GET",
             payload: dict[str, Any] | None = None, structured_errors: bool = False) -> bytes:
    request_path = path
    if query:
        request_path = f"{path}?{urlencode({key: value for key, value in query.items() if value})}"
    try:
        response, _ = auth.authenticated_request(request_path, method=method, payload=payload, timeout=60)
        return json.dumps(response).encode("utf-8")
    except auth.AuthError as exc:
        if structured_errors:
            raise MarketplaceRequestError(
                exc.code, exc.status, exc.code in _RETRYABLE_MARKETPLACE_CODES,
            ) from exc
        raise RuntimeError(f"Marketplace request failed ({exc.code}).") from exc


def search(*, query: str = "", asset: str = "", venue: str = "", category: str = "", data_type: str = "", risk_profile: str = "", min_capacity_usd: float | None = None, sort: str = "published", locale: str = "", page: int = 1, page_size: int = 30) -> dict[str, Any]:
    normalized_risk_profile = RISK_PROFILE_ALIASES.get(risk_profile.strip().lower(), risk_profile.strip().lower())
    if not isinstance(page, int) or isinstance(page, bool) or page < 1:
        raise ValueError("Marketplace page must be a positive integer")
    if not isinstance(page_size, int) or isinstance(page_size, bool) or page_size < 1 or page_size > 100:
        raise ValueError("Marketplace page size must be between 1 and 100")
    payload = _request("/api/live/strategies", {"q": query, "asset": asset, "venue": venue, "category": category, "data_type": data_type, "risk_profile": normalized_risk_profile, "min_capacity_usd": "" if min_capacity_usd is None else str(min_capacity_usd), "sort": sort, "locale": locale, "page": str(page), "page_size": str(page_size)})
    try:
        return json.loads(payload)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Marketplace returned an invalid response.") from exc


def inspect(slug: str, version: str, *, locale: str = "") -> dict[str, Any]:
    payload = _request(f"/api/live/strategies/{slug}/{version}", {"locale": locale})
    try:
        return json.loads(payload)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Marketplace returned an invalid response.") from exc


def versions(slug: str) -> dict[str, Any]:
    payload = _request(f"/api/live/strategies/{slug}/versions")
    try:
        return json.loads(payload)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Marketplace returned an invalid response.") from exc


def recommend(questionnaire: dict[str, Any]) -> dict[str, Any]:
    """Return the server-ranked three-card recommendation for one V2 questionnaire."""
    payload = _request(
        "/api/live/recommendations", method="POST", payload=questionnaire, structured_errors=True,
    )
    try:
        result = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Marketplace returned an invalid response.") from exc
    if not isinstance(result, dict):
        raise RuntimeError("Marketplace returned an invalid response.")
    return result


def _retry_after(headers: Any) -> int | None:
    raw = headers.get("Retry-After") if headers is not None else None
    try:
        value = int(raw or "")
    except (TypeError, ValueError):
        return None
    return value if 1 <= value <= MAX_RETRY_AFTER else None


def _quota_period(value: object, expected_limit: int) -> dict[str, Any] | None:
    if not isinstance(value, dict) or set(value) != {"limit", "used", "resets_at"}:
        return None
    limit, used, resets_at = value.get("limit"), value.get("used"), value.get("resets_at")
    if limit != expected_limit or not isinstance(used, int) or isinstance(used, bool) or not 0 <= used <= limit:
        return None
    if not isinstance(resets_at, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z", resets_at):
        return None
    try:
        datetime.fromisoformat(resets_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    return {"limit": limit, "used": used, "resets_at": resets_at}


def _download_request_error(exc: HTTPError) -> MarketplaceRequestError:
    retry_after = _retry_after(exc.headers)
    try:
        raw = exc.read(MAX_ERROR_BYTES + 1)
    finally:
        exc.close()
    if len(raw) > MAX_ERROR_BYTES:
        return MarketplaceRequestError("REMOTE_ERROR", exc.code, exc.code >= 500, retry_after=retry_after)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return MarketplaceRequestError("REMOTE_ERROR", exc.code, exc.code >= 500, retry_after=retry_after)
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict) or not isinstance(error.get("code"), str):
        return MarketplaceRequestError("REMOTE_ERROR", exc.code, exc.code >= 500, retry_after=retry_after)
    code = error["code"]
    if code == "DOWNLOAD_QUOTA_EXCEEDED" and exc.code == 429 and retry_after is not None \
            and error.get("retryable") is True:
        scopes = error.get("limiting_scopes")
        quotas = error.get("quotas")
        if isinstance(scopes, list) and scopes and len(scopes) == len(set(scopes)) \
                and all(scope in {"network", "live_user"} for scope in scopes) \
                and isinstance(quotas, dict) and set(quotas) == {"network", "live_user"}:
            parsed_quotas: dict[str, dict[str, dict[str, Any]]] = {}
            for scope in ("network", "live_user"):
                quota = quotas.get(scope)
                if not isinstance(quota, dict) or set(quota) != {"daily", "monthly"}:
                    break
                daily = _quota_period(quota.get("daily"), 20)
                monthly = _quota_period(quota.get("monthly"), 100)
                if daily is None or monthly is None:
                    break
                parsed_quotas[scope] = {"daily": daily, "monthly": monthly}
            exhausted = {scope for scope, quota in parsed_quotas.items()
                         if quota["daily"]["used"] >= quota["daily"]["limit"]
                         or quota["monthly"]["used"] >= quota["monthly"]["limit"]}
            if len(parsed_quotas) == 2 and set(scopes) == exhausted:
                return MarketplaceRequestError(code, 429, True, retry_after=retry_after,
                                               limiting_scopes=scopes, quotas=parsed_quotas)
        return MarketplaceRequestError("REMOTE_ERROR", 429, False, retry_after=retry_after)
    if code == "DOWNLOAD_QUOTA_UNAVAILABLE":
        if exc.code == 503 and error.get("retryable") is True:
            return MarketplaceRequestError(code, 503, True, retry_after=retry_after)
        return MarketplaceRequestError("REMOTE_ERROR", exc.code, exc.code >= 500, retry_after=retry_after)
    allowed = {
        "RATE_LIMITED": exc.code == 429,
        "AUTH_REQUIRED": exc.code == 401,
        "INVALID_TOKEN": exc.code == 401,
        "INSUFFICIENT_SCOPE": exc.code == 403,
    }
    if allowed.get(code):
        return MarketplaceRequestError(
            code, exc.code, code == "RATE_LIMITED",
            retry_after=retry_after,
        )
    return MarketplaceRequestError("REMOTE_ERROR", exc.code, exc.code >= 500, retry_after=retry_after)


def _download(slug: str, version: str) -> bytes:
    """Stream an archive to disk and validate declared and actual integrity."""
    url = f"{marketplace_origin()}/api/live/strategies/{slug}/{version}/download"
    temporary_path: Path | None = None
    try:
        token = auth.access_token()
        for attempt in range(2):
            try:
                response_context = urlopen(Request(url, method="GET", headers={
                    "User-Agent": "EdgePilot Marketplace/1.0", "Authorization": f"Bearer {token}",
                }), timeout=60)
                break
            except HTTPError as exc:
                if exc.code == 401 and attempt == 0:
                    try:
                        token = auth.refresh_access_token()
                    finally:
                        exc.close()
                    continue
                raise
        else:
            raise RuntimeError("Marketplace authentication failed.")
        with response_context as response:
            raw_length = response.headers.get("Content-Length")
            expected_sha256 = response.headers.get("X-Package-SHA256")
            try:
                expected_length = int(raw_length or "")
            except ValueError as exc:
                raise RuntimeError("Marketplace download has an invalid Content-Length.") from exc
            if expected_length < 1 or expected_length > MAX_ARCHIVE_BYTES:
                raise RuntimeError("Marketplace package exceeds the 20 MiB download limit.")
            if not expected_sha256 or not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256):
                raise RuntimeError("Marketplace download has an invalid SHA-256 header.")
            root = strategies_root()
            root.mkdir(parents=True, exist_ok=True)
            descriptor, raw_path = tempfile.mkstemp(prefix=".marketplace-download-", suffix=".zip", dir=root)
            temporary_path = Path(raw_path)
            digest = hashlib.sha256()
            received = 0
            with os.fdopen(descriptor, "wb") as output:
                while True:
                    chunk = response.read(min(1024 * 1024, MAX_ARCHIVE_BYTES + 1 - received))
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > MAX_ARCHIVE_BYTES:
                        raise RuntimeError("Marketplace package exceeds the 20 MiB download limit.")
                    output.write(chunk)
                    digest.update(chunk)
                output.flush()
                os.fsync(output.fileno())
            if received != expected_length:
                raise RuntimeError("Marketplace download length does not match Content-Length.")
            if digest.hexdigest() != expected_sha256.lower():
                raise RuntimeError("Marketplace download SHA-256 does not match the published package.")
            return temporary_path.read_bytes()
    except HTTPError as exc:
        raise _download_request_error(exc) from exc
    except URLError as exc:
        raise MarketplaceRequestError("SERVICE_UNAVAILABLE", None, True) from exc
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _current_user() -> tuple[str, str]:
    current = auth.status()
    user = current.get("user")
    if not current.get("authenticated") or not isinstance(user, dict) or not isinstance(user.get("id"), (str, int)):
        raise auth.AuthError("AUTH_REQUIRED")
    return str(user["id"]), auth.access_token()


def download_and_install(slug: str, version: str) -> dict[str, str]:
    preflight_install(slug, version)
    user_id, token = _current_user()
    archive = _download(slug, version)
    installed_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    key = auth.prepare_installation(user_id, slug, version, installed_at)
    result = install_package(slug, version, archive)
    auth.update_pending_installation(key, "installed")
    try:
        auth.sync_pending_installations(token=token, user_id=user_id)
    except auth.AuthError:
        pass
    return result


def installation_history() -> dict[str, Any]:
    user_id, _ = _current_user()
    payload, _ = auth.authenticated_request("/api/account/installations")
    payload["pending_sync"] = auth.pending_installation_counts(user_id)
    payload["local_sync_issues"] = auth.pending_installation_issues(user_id)
    return payload


def clear_installation_history(strategy_slug: str) -> dict[str, Any]:
    user_id, _ = _current_user()
    payload, _ = auth.authenticated_request(f"/api/account/installations/{quote(strategy_slug, safe='')}", method="DELETE")
    auth.clear_pending_installations(user_id, strategy_slug)
    return payload


def restore(*, strategy_slug: str = "") -> dict[str, Any]:
    history = installation_history()
    rows = history.get("installations")
    if not isinstance(rows, list):
        raise RuntimeError("Marketplace returned invalid installation history.")
    results: list[dict[str, str]] = []
    selected = [row for row in rows if isinstance(row, dict) and (not strategy_slug or row.get("strategy_slug") == strategy_slug)]
    if strategy_slug and not selected:
        return {"restored": [], "results": [{"strategy_slug": strategy_slug, "status": "not_in_history"}]}
    for row in selected:
        slug = row.get("strategy_slug")
        version = row.get("last_installed_version")
        if not isinstance(slug, str) or not isinstance(version, str):
            continue
        marker = strategies_root() / slug.replace("-", "_") / ".marketplace.json"
        if marker.exists():
            try:
                local = json.loads(marker.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                local = {}
            status_value = "already_installed" if local.get("slug") == slug and local.get("version") == version else "conflict"
            results.append({"strategy_slug": slug, "version": version, "status": status_value})
            continue
        destination = strategies_root() / slug.replace("-", "_")
        if destination.exists():
            results.append({"strategy_slug": slug, "version": version, "status": "conflict"})
            continue
        try:
            download_and_install(slug, version)
        except MarketplaceRequestError as exc:
            if exc.code == "DOWNLOAD_QUOTA_EXCEEDED":
                results.append({"strategy_slug": slug, "version": version, "status": "quota_exceeded"})
                return {
                    "restored": [item["strategy_slug"] for item in results if item["status"] == "restored"],
                    "results": results,
                    "quota_exceeded": {"code": exc.code, **exc.public_details()},
                }
            results.append({"strategy_slug": slug, "version": version, "status": "unavailable"})
            continue
        except RuntimeError:
            results.append({"strategy_slug": slug, "version": version, "status": "unavailable"})
            continue
        results.append({"strategy_slug": slug, "version": version, "status": "restored"})
    return {"restored": [item["strategy_slug"] for item in results if item["status"] == "restored"], "results": results}
