from __future__ import annotations

from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, cast

from analytics_toolkit.general.connections import (
    CONNECTIONS_FILE_NAME,
    get_connections_path_override,
    get_last_connections_path,
    remember_connections_path,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator


def find_connections_file_path() -> Path | None:
    """Find `.connections` using remembered, caller, then working directories."""
    previous_path = get_connections_path_override() or get_last_connections_path()
    search_roots: list[Path] = []
    if previous_path is not None:
        search_roots.append(previous_path.parent)

    caller_dir = _resolve_calling_base_dir()
    if caller_dir is not None:
        search_roots.append(caller_dir)
    search_roots.append(Path.cwd())

    for directory in _iter_search_directories(search_roots):
        connections_path = directory / CONNECTIONS_FILE_NAME
        if connections_path.is_file():
            return remember_connections_path(connections_path)
    return None


def _resolve_calling_base_dir() -> Path | None:
    read_file_module = import_module("analytics_toolkit.general.read_file")
    resolver = cast(
        "Callable[[], Path | None]",
        vars(read_file_module)["_resolve_base_dir"],
    )
    return resolver()


def _iter_search_directories(search_roots: list[Path]) -> Iterator[Path]:
    seen: set[Path] = set()
    for search_root in search_roots:
        resolved_root = search_root.expanduser().resolve()
        for directory in (resolved_root, *resolved_root.parents):
            if directory in seen:
                continue
            seen.add(directory)
            yield directory
