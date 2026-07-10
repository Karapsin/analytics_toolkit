from __future__ import annotations

from pathlib import Path


CONNECTIONS_FILE_NAME = ".connections"
_CONNECTIONS_PATH_OVERRIDE: Path | None = None


def set_connections_path(path: str | Path | None) -> Path | None:
    """Set or clear the explicit SQL `.connections` file path."""
    global _CONNECTIONS_PATH_OVERRIDE

    if path is None:
        _CONNECTIONS_PATH_OVERRIDE = None
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

    _CONNECTIONS_PATH_OVERRIDE = connections_path
    return connections_path


def get_connections_path_override() -> Path | None:
    return _CONNECTIONS_PATH_OVERRIDE
