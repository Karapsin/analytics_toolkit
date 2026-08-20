from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


CONNECTIONS_FILE_NAME = ".connections"


@dataclass
class _ConnectionsPathState:
    override: Path | None = None
    last: Path | None = None


_CONNECTIONS_PATH_STATE = _ConnectionsPathState()


def set_connections_path(path: str | Path | None) -> Path | None:
    """Set or clear the explicit SQL `.connections` file path."""
    if path is None:
        _CONNECTIONS_PATH_STATE.override = None
        _CONNECTIONS_PATH_STATE.last = None
        return None

    raw_path = Path(path).expanduser()
    if raw_path.name != CONNECTIONS_FILE_NAME:
        raise ValueError(
            "SQL connections path must point to a .connections file: "
            f"{raw_path}"
        )
    connections_path = raw_path.resolve()
    if not connections_path.is_file():
        raise ValueError(
            "SQL connections path must be an existing .connections file: "
            f"{connections_path}"
        )

    _CONNECTIONS_PATH_STATE.override = connections_path
    _CONNECTIONS_PATH_STATE.last = connections_path
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
