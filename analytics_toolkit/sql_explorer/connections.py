"""Connection-file selection and persisted Explorer launch state."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from analytics_toolkit import general, sql
from analytics_toolkit.general.connections import (
    get_connections_path_override,
    get_last_connections_path,
    remember_connections_path,
)

from .errors import SqlExplorerConfigurationError
from .settings import load_settings, save_settings

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True)
class ConnectionsRestart:
    path: Path


def check_connections_file(path: Path) -> Path:
    """Check an explicit candidate without changing the running SQL source."""
    resolved = path.expanduser().resolve()
    if resolved.name != ".connections" or not resolved.is_file():
        message = "Choose an existing .connections file."
        raise SqlExplorerConfigurationError(message)
    raw = json.loads(resolved.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        message = ".connections must contain a JSON object."
        raise SqlExplorerConfigurationError(message)
    return resolved


def activate_connections_file(path: Path) -> Path:
    """Validate and remember a source only after all old workers have stopped."""
    candidate = check_connections_file(path)
    previous_override = get_connections_path_override()
    previous_last = get_last_connections_path()
    try:
        general.set_connections_path(candidate)
        _require_valid_connections()
        loaded = load_settings()
        save_settings(replace(loaded.settings, connections_path=str(candidate)))
    except Exception:
        general.set_connections_path(None)
        if previous_override is not None and previous_override.is_file():
            general.set_connections_path(previous_override)
        elif previous_last is not None:
            remember_connections_path(previous_last)
        raise
    return candidate


def _require_valid_connections() -> None:
    choices = sql.validate_connections(connect=False)
    if not any(choice.valid and choice.backend is not None for choice in choices):
        message = "No valid SQL connections were found in .connections."
        raise SqlExplorerConfigurationError(message)
