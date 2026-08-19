from __future__ import annotations

from dataclasses import dataclass
import importlib
import importlib.util
import inspect
import logging
from pathlib import Path
import pkgutil
import sys
from types import UnionType
from typing import Any
from typing import Union
from typing import get_args
from typing import get_origin
from typing import get_type_hints

from nautilus_trader.config import StrategyConfig
from nautilus_trader.common.config import NautilusConfig
from nautilus_trader.live.config import LiveDataClientConfig
from nautilus_trader.live.config import LiveExecClientConfig
from nautilus_trader.live.factories import LiveDataClientFactory
from nautilus_trader.live.factories import LiveExecClientFactory
from nautilus_trader.trading.strategy import Strategy
from edgepilot_backtest_core.discovery import StrategyDescriptor
from edgepilot_backtest_core.discovery import instantiate_config as _core_instantiate_config
from edgepilot_backtest_core.discovery import instantiate_config_class as _core_instantiate_config_class
from edgepilot_backtest_core.discovery import resolve_strategy as _core_resolve_strategy
from edgepilot_backtest_core.discovery import strategy_names as _core_strategy_names

from edgepilot.paths import state_root


LOGGER = logging.getLogger("edgepilot.discovery")


@dataclass(frozen=True)
class AdapterDescriptor:
    name: str
    data_config_path: str
    data_factory_path: str
    exec_config_path: str | None
    exec_factory_path: str | None


def _class_path(cls: type[Any]) -> str:
    return f"{cls.__module__}:{cls.__qualname__}"


def strategies_root() -> Path:
    """Return the user-owned strategy directory without installing packages."""
    persistent = state_root() / "strategies"
    persistent.mkdir(parents=True, exist_ok=True)
    init = persistent / "__init__.py"
    if not init.exists():
        init.write_text("", encoding="utf-8")
    return persistent


def _ensure_project_importable() -> None:
    root = str(state_root())
    if root not in sys.path:
        sys.path.insert(0, root)


def _strategy_package_dir(name: str) -> Path | None:
    root = strategies_root()
    direct = root / name
    if direct.is_dir() and (direct / "__init__.py").exists():
        return direct
    underscored = root / name.replace("-", "_")
    if underscored.is_dir() and (underscored / "__init__.py").exists():
        return underscored
    return None


def _import_strategy_package(name: str):
    _ensure_project_importable()
    package_dir = _strategy_package_dir(name)
    if package_dir is None:
        raise ModuleNotFoundError(f"No strategy package named {name!r}")

    module_key = f"strategies.{name.replace('-', '_')}"
    if module_key in sys.modules:
        return sys.modules[module_key]

    init_path = package_dir / "__init__.py"
    spec = importlib.util.spec_from_file_location(
        module_key,
        init_path,
        submodule_search_locations=[str(package_dir)],
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load strategy package {name!r} from {init_path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_key] = module
    spec.loader.exec_module(module)
    return module


def strategy_names() -> list[str]:
    return _core_strategy_names(strategies_root())


def resolve_strategy(name_or_path: str, config_path: str | None = None) -> StrategyDescriptor:
    """Resolve a local strategy name or a fully-qualified Nautilus strategy path."""
    return _core_resolve_strategy(
        name_or_path,
        strategies_root=strategies_root(),
        config_path=config_path,
    )

    # Kept below unreachable only until downstream imports have migrated.
    _ensure_project_importable()
    if ":" in name_or_path:
        module_name, class_name = name_or_path.split(":", 1)
        module = importlib.import_module(module_name)
        strategy_cls = getattr(module, class_name)
        display_name = class_name
    else:
        module = _import_strategy_package(name_or_path)
        module_name = module.__name__
        display_name = name_or_path
        candidates = [
            cls
            for _, cls in inspect.getmembers(module, inspect.isclass)
            if issubclass(cls, Strategy)
            and cls is not Strategy
            and cls.__module__.startswith(module.__name__)
        ]
        if len(candidates) != 1:
            raise ValueError(
                f"Strategy module {module_name!r} must define exactly one Strategy subclass; "
                f"found {[cls.__name__ for cls in candidates]}",
            )
        strategy_cls = candidates[0]

    if not issubclass(strategy_cls, Strategy):
        raise TypeError(f"{strategy_cls!r} is not a NautilusTrader Strategy")

    if config_path:
        config_module_name, config_name = config_path.split(":", 1)
        config_cls = getattr(importlib.import_module(config_module_name), config_name)
    else:
        config_candidates = [
            cls
            for _, cls in inspect.getmembers(module, inspect.isclass)
            if issubclass(cls, StrategyConfig)
            and cls is not StrategyConfig
            and cls.__module__.startswith(module.__name__)
        ]
        if len(config_candidates) != 1:
            raise ValueError(
                f"Strategy module {module.__name__!r} must define exactly one StrategyConfig subclass; "
                f"found {[cls.__name__ for cls in config_candidates]}",
            )
        config_cls = config_candidates[0]

    return StrategyDescriptor(
        name=display_name,
        strategy_path=_class_path(strategy_cls),
        config_path=_class_path(config_cls),
        strategy_cls=strategy_cls,
        config_cls=config_cls,
    )


def _single_subclass(module: Any, base: type[Any], label: str, required: bool = True):
    candidates = [
        cls
        for _, cls in inspect.getmembers(module, inspect.isclass)
        if issubclass(cls, base) and cls is not base and cls.__module__.startswith(module.__name__)
    ]
    if not candidates and not required:
        return None
    if len(candidates) != 1:
        raise ValueError(
            f"Adapter {module.__name__!r} exposes {len(candidates)} {label} classes; "
            "supply explicit native class paths",
        )
    return candidates[0]


def resolve_adapter(name: str) -> AdapterDescriptor:
    """Discover native client configs and factories from an installed Nautilus adapter."""
    normalized = name.lower().replace("-", "_")
    config_module = importlib.import_module(f"nautilus_trader.adapters.{normalized}.config")
    factory_module = importlib.import_module(f"nautilus_trader.adapters.{normalized}.factories")

    data_config = _single_subclass(config_module, LiveDataClientConfig, "data config")
    data_factory = _single_subclass(factory_module, LiveDataClientFactory, "data factory")
    exec_config = _single_subclass(config_module, LiveExecClientConfig, "execution config", required=False)
    exec_factory = _single_subclass(factory_module, LiveExecClientFactory, "execution factory", required=False)

    return AdapterDescriptor(
        name=name.upper(),
        data_config_path=_class_path(data_config),
        data_factory_path=_class_path(data_factory),
        exec_config_path=_class_path(exec_config) if exec_config else None,
        exec_factory_path=_class_path(exec_factory) if exec_factory else None,
    )


def discover_execution_adapters() -> list[AdapterDescriptor]:
    """Return executable adapters provided by the installed NautilusTrader wheel."""
    adapters_package = importlib.import_module("nautilus_trader.adapters")
    discovered: list[AdapterDescriptor] = []
    for module in sorted(pkgutil.iter_modules(adapters_package.__path__), key=lambda item: item.name):
        if module.name.startswith("_"):
            continue
        try:
            adapter = resolve_adapter(module.name)
        except (ImportError, ModuleNotFoundError, ValueError, TypeError, AttributeError) as exc:
            LOGGER.debug(
                "native adapter skipped",
                extra={"event": "adapter.discovery.skipped", "params": {"adapter": module.name, "error": type(exc).__name__}},
            )
            continue
        if adapter.exec_config_path is not None and adapter.exec_factory_path is not None:
            discovered.append(adapter)
    return discovered


def _coerce_native(value: Any, annotation: Any) -> Any:
    if annotation is Any or annotation is None:
        return value
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin in (UnionType, Union):
        target = next((arg for arg in args if arg is not type(None)), Any)
        return _coerce_native(value, target)
    if origin in (list, tuple, set, frozenset):
        item_type = args[0] if args else Any
        converted = [_coerce_native(item, item_type) for item in value]
        return origin(converted) if origin is not tuple else tuple(converted)
    if inspect.isclass(annotation):
        if isinstance(value, annotation):
            return value
        if isinstance(value, dict) and issubclass(annotation, NautilusConfig):
            return instantiate_config_class(annotation, value)
        if isinstance(value, str):
            member = getattr(annotation, value.upper(), None)
            if member is not None:
                return member
            from_str = getattr(annotation, "from_str", None)
            if from_str is not None:
                return from_str(value)
    return value


def instantiate_config_class(config_cls: type[Any], values: dict[str, Any]) -> Any:
    return _core_instantiate_config_class(config_cls, values)


def instantiate_config(path: str, values: dict[str, Any]) -> Any:
    return _core_instantiate_config(path, values)
