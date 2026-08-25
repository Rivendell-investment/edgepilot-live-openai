from __future__ import annotations

from dataclasses import dataclass
import importlib
import importlib.util
import inspect
from pathlib import Path
import sys
from types import UnionType
from typing import Any, Union, get_args, get_origin, get_type_hints

from nautilus_trader.common.config import NautilusConfig
from nautilus_trader.config import StrategyConfig
from nautilus_trader.trading.strategy import Strategy


@dataclass(frozen=True)
class StrategyDescriptor:
    name: str
    strategy_path: str
    config_path: str
    strategy_cls: type[Strategy]
    config_cls: type[StrategyConfig]


def _class_path(cls: type[Any]) -> str:
    return f"{cls.__module__}:{cls.__qualname__}"


def strategy_names(root: Path) -> list[str]:
    if not root.exists():
        return []
    names = [
        path.stem for path in root.glob("*.py")
        if path.name != "__init__.py" and not path.name.startswith("_")
    ]
    names.extend(
        path.name for path in root.iterdir()
        if path.is_dir() and (path / "__init__.py").exists() and not path.name.startswith("_")
    )
    return sorted(set(names))


def _import_strategy_package(name: str, root: Path):
    package_dir = root / name
    if not (package_dir.is_dir() and (package_dir / "__init__.py").exists()):
        package_dir = root / name.replace("-", "_")
    if not (package_dir.is_dir() and (package_dir / "__init__.py").exists()):
        raise ModuleNotFoundError(f"No strategy package named {name!r}")
    project_root = str(root.parent)
    if project_root not in sys.path:
        sys.path.insert(0, project_root)
    module_key = f"strategies.{name.replace('-', '_')}"
    if module_key in sys.modules:
        return sys.modules[module_key]
    spec = importlib.util.spec_from_file_location(
        module_key, package_dir / "__init__.py", submodule_search_locations=[str(package_dir)],
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load strategy package {name!r} from {package_dir}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_key] = module
    spec.loader.exec_module(module)
    return module


def resolve_strategy(
    name_or_path: str,
    *,
    strategies_root: Path | None = None,
    config_path: str | None = None,
) -> StrategyDescriptor:
    if ":" in name_or_path:
        module_name, class_name = name_or_path.split(":", 1)
        module = importlib.import_module(module_name)
        strategy_cls = getattr(module, class_name)
        display_name = class_name
    else:
        if strategies_root is None:
            raise ValueError("strategies_root is required when resolving a strategy name")
        module = _import_strategy_package(name_or_path, strategies_root)
        display_name = name_or_path
        candidates = _subclasses(module, Strategy)
        if len(candidates) != 1:
            raise ValueError(
                f"Strategy module {module.__name__!r} must define exactly one Strategy subclass; "
                f"found {[cls.__name__ for cls in candidates]}",
            )
        strategy_cls = candidates[0]
    if not inspect.isclass(strategy_cls) or not issubclass(strategy_cls, Strategy):
        raise TypeError(f"{strategy_cls!r} is not a NautilusTrader Strategy")
    if config_path:
        config_module_name, config_name = config_path.split(":", 1)
        config_cls = getattr(importlib.import_module(config_module_name), config_name)
    else:
        candidates = _subclasses(module, StrategyConfig)
        if len(candidates) != 1:
            raise ValueError(
                f"Strategy module {module.__name__!r} must define exactly one StrategyConfig subclass; "
                f"found {[cls.__name__ for cls in candidates]}",
            )
        config_cls = candidates[0]
    return StrategyDescriptor(display_name, _class_path(strategy_cls), _class_path(config_cls), strategy_cls, config_cls)


def _subclasses(module: Any, base: type[Any]) -> list[type[Any]]:
    return [
        cls for _, cls in inspect.getmembers(module, inspect.isclass)
        if issubclass(cls, base) and cls is not base and cls.__module__.startswith(module.__name__)
    ]


def _coerce_native(value: Any, annotation: Any) -> Any:
    if annotation is Any or annotation is None:
        return value
    origin, args = get_origin(annotation), get_args(annotation)
    if origin in (UnionType, Union):
        return _coerce_native(value, next((arg for arg in args if arg is not type(None)), Any))
    if origin in (list, tuple, set, frozenset):
        converted = [_coerce_native(item, args[0] if args else Any) for item in value]
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
            if getattr(annotation, "from_str", None) is not None:
                return annotation.from_str(value)
    return value


def instantiate_config_class(config_cls: type[Any], values: dict[str, Any]) -> Any:
    hints = get_type_hints(config_cls)
    return config_cls(**{key: _coerce_native(value, hints.get(key, Any)) for key, value in values.items()})


def instantiate_config(path: str, values: dict[str, Any]) -> Any:
    module_name, class_name = path.split(":", 1)
    return instantiate_config_class(getattr(importlib.import_module(module_name), class_name), values)
