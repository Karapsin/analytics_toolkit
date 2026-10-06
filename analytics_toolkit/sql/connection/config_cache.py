from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING, TypeVar, cast

from analytics_toolkit.general.connections import (
    connections_path_lock,
    get_connections_path_revision,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

T = TypeVar("T")


@dataclass(frozen=True)
class _CachedConnectionsSource:
    revision: int
    path: Path
    source: object


@dataclass
class _ConnectionsSourceCache:
    source: _CachedConnectionsSource | None = None


_SOURCE_CACHE = _ConnectionsSourceCache()


def _current_source() -> _CachedConnectionsSource | None:
    source = _SOURCE_CACHE.source
    if source is not None and source.revision != get_connections_path_revision():
        _SOURCE_CACHE.source = None
    return _SOURCE_CACHE.source


def get_cached_connections_path() -> Path | None:
    """Return the active snapshot's path without touching the filesystem."""
    with connections_path_lock():
        source = _current_source()
        return source.path if source is not None else None


def load_cached_connections_source(
    loader: Callable[[], tuple[Path, T]],
) -> tuple[Path, T]:
    """Publish a successful parse once per selection; give callers their own copy."""
    with connections_path_lock():
        source = _current_source()
        if source is None:
            path, parsed = loader()
            source = _CachedConnectionsSource(
                revision=get_connections_path_revision(), path=path, source=parsed
            )
            _SOURCE_CACHE.source = source
        return source.path, deepcopy(cast("T", source.source))
