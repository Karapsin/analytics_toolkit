from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator


CONNECTIONS_FILE_NAME = ".connections"


@dataclass
class _ConnectionsPathState:
    override: Path | None = None
    last: Path | None = None
    revision: int = 0


_CONNECTIONS_PATH_STATE = _ConnectionsPathState()
_CONNECTIONS_PATH_LOCK = RLock()


@contextmanager
def connections_path_lock() -> Iterator[None]:
    """Serialize explicit selection and SQL configuration cache initialization."""
    with _CONNECTIONS_PATH_LOCK:
        yield


def get_connections_path_revision() -> int:
    with _CONNECTIONS_PATH_LOCK:
        return _CONNECTIONS_PATH_STATE.revision


def set_connections_path(path: str | Path | None) -> Path | None:
    """Select or clear the SQL source, invalidating its cached settings."""
    with _CONNECTIONS_PATH_LOCK:
        return _set_connections_path(path)


def _set_connections_path(path: str | Path | None) -> Path | None:
    if path is None:
        _CONNECTIONS_PATH_STATE.override = None
        _CONNECTIONS_PATH_STATE.last = None
        _CONNECTIONS_PATH_STATE.revision += 1
        return None

    raw_path = Path(path).expanduser()
    if raw_path.name != CONNECTIONS_FILE_NAME:
        raise ValueError(f"SQL connections path must point to a .connections file: {raw_path}")
    connections_path = raw_path.resolve()
    if not connections_path.is_file():
        raise ValueError(
            f"SQL connections path must be an existing .connections file: {connections_path}"
        )

    _CONNECTIONS_PATH_STATE.override = connections_path
    _CONNECTIONS_PATH_STATE.last = connections_path
    _CONNECTIONS_PATH_STATE.revision += 1
    return connections_path


def get_connections_path_override() -> Path | None:
    return _CONNECTIONS_PATH_STATE.override


def get_last_connections_path() -> Path | None:
    return _CONNECTIONS_PATH_STATE.last


def remember_connections_path(path: Path) -> Path:
    """Remember a successful lookup and promote an active override."""
    connections_path = path.expanduser().resolve()
    if _CONNECTIONS_PATH_STATE.override is not None:
        _CONNECTIONS_PATH_STATE.override = connections_path
    _CONNECTIONS_PATH_STATE.last = connections_path
    return connections_path
