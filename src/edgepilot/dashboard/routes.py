"""Method-aware matching for parameterized localhost Dashboard routes."""

from __future__ import annotations

from dataclasses import dataclass
import re


@dataclass(frozen=True)
class RouteMatch:
    name: str
    params: tuple[str, ...]


_ROUTES: dict[str, tuple[tuple[str, re.Pattern[str]], ...]] = {
    "GET": (
        ("marketplace_versions", re.compile(r"^/api/marketplace/strategies/([^/]+)/versions$")),
        ("marketplace_detail", re.compile(r"^/api/marketplace/strategies/([^/]+)/([^/]+)$")),
        ("strategy_config", re.compile(r"^/api/strategies/([^/]+)/configs/([^/]+)$")),
        ("strategy_workspace", re.compile(r"^/api/strategies/([^/]+)/workspace$")),
        ("strategy_deployment_preflight", re.compile(r"^/api/strategies/([^/]+)/deployment-preflight$")),
        ("strategy_detail", re.compile(r"^/api/strategies/(?:.*/)?([^/]*)$")),
        ("job_detail", re.compile(r"^/api/jobs/(?:.*/)?([^/]*)$")),
        ("run_chart", re.compile(r"^/api/runs/([^/]+)(?:/.*)?/chart$")),
        ("run_runtime", re.compile(r"^/api/runs/([^/]+)(?:/.*)?/runtime$")),
        ("run_detail", re.compile(r"^/api/runs/(?:.*/)?([^/]*)$")),
    ),
    "POST": (
        ("strategy_configurations", re.compile(r"^/api/strategies/([^/]+)/configurations$")),
        ("strategy_config", re.compile(r"^/api/strategies/([^/]+)/configs/([^/]+)$")),
        ("job_stop", re.compile(r"^/api/jobs/([^/]+)(?:/.*)?/stop$")),
        ("run_emergency_stop", re.compile(r"^/api/runs/([^/]+)(?:/.*)?/emergency-stop$")),
    ),
    "PUT": (
        ("strategy_configuration", re.compile(r"^/api/strategies/([^/]+)/configurations/([^/]+)$")),
        ("strategy_config", re.compile(r"^/api/strategies/([^/]+)/configs/([^/]+)$")),
    ),
    "DELETE": (
        ("strategy_configuration", re.compile(r"^/api/strategies/([^/]+)/configurations/([^/]+)$")),
        ("marketplace_history", re.compile(r"^/api/marketplace/history/([^/]+)$")),
        ("strategy", re.compile(r"^/api/strategies/([^/]+)$")),
        ("catalog_dataset", re.compile(r"^/api/catalog/([^/]+)/([^/]+)$")),
        ("run", re.compile(r"^/api/runs/([^/]+)$")),
    ),
}


def match_route(method: str, path: str) -> RouteMatch | None:
    for name, pattern in _ROUTES.get(method.upper(), ()):
        matched = pattern.fullmatch(path)
        if matched:
            return RouteMatch(name=name, params=matched.groups())
    return None
