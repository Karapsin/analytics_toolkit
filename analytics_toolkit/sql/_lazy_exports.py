from __future__ import annotations

from importlib import import_module
from typing import Any, Mapping, Tuple


ExportMap = Mapping[str, Tuple[str, str]]


def resolve_lazy_export(name: str, exports: ExportMap, module_name: str) -> Any:
    try:
        target_module, target_name = exports[name]
    except KeyError as exc:
        raise AttributeError(
            f"module {module_name!r} has no attribute {name!r}"
        ) from exc
    return getattr(import_module(target_module), target_name)


def lazy_export_dir(module_globals: dict[str, Any], exports: ExportMap) -> list[str]:
    return sorted({*module_globals, *exports})
