from __future__ import annotations

import sys
from pathlib import Path
from typing import TextIO

from analytics_toolkit import sql
from analytics_toolkit.general.connections import get_connections_path_override

from .connections import ConnectionsRestart, activate_connections_file
from .errors import (
    SqlExplorerConfigurationError,
    SqlExplorerDependencyError,
    SqlExplorerEnvironmentError,
)
from .runtime import ExplorerSession
from .settings import load_settings


def run(db_key: str | None = None) -> None:
    """Launch the exploratory SQL TUI, optionally selecting a database key."""
    _require_terminal(sys.stdin, sys.stdout)
    previous_sink = sql.get_time_print_sink()
    sql.set_time_print_sink("logging")
    try:
        try:
            # Imports stay inside the launcher so the base package needs no TUI dependencies.
            from .app import DatabasePickerApp, SqlExplorerApp  # noqa: PLC0415
        except ModuleNotFoundError as exc:
            if exc.name in {"pyperclip", "textual"}:
                message = (
                    "SQL explorer requires optional TUI dependencies. "
                    "Install them with: pip install 'analytics-toolkit[tui]'"
                )
                raise SqlExplorerDependencyError(message) from exc
            raise
        from .terminal_keys import install_terminal_key_compatibility  # noqa: PLC0415

        install_terminal_key_compatibility()
        requested_path: Path | None = None
        while _prepare_connections(requested_path):
            if db_key is None:
                choices = _database_choices()
                db_key = DatabasePickerApp(choices).run()
                if db_key is None:
                    return
            session = ExplorerSession(db_key)
            result = SqlExplorerApp(session).run()
            if not isinstance(result, ConnectionsRestart):
                return
            requested_path = result.path
            db_key = None
    finally:
        sql.set_time_print_sink(previous_sink)


def _prepare_connections(requested: Path | None = None) -> bool:
    from .connections_picker import ConnectionsPickerApp  # noqa: PLC0415

    explicit = get_connections_path_override()
    if requested is None and explicit is not None and explicit.is_file():
        return True
    saved = load_settings().settings.connections_path
    candidate = requested or (Path(saved) if saved else None)
    error = ""
    auto_select = candidate is None or not candidate.is_file()
    while True:
        if candidate is not None and candidate.is_file():
            try:
                activate_connections_file(candidate)
            except (OSError, ValueError, RuntimeError) as exc:
                error = str(exc)
                auto_select = False
            else:
                return True
        candidate = ConnectionsPickerApp(auto_select=auto_select, error=error).run()
        if candidate is None:
            return False
        auto_select = False


def _require_terminal(stdin: TextIO, stdout: TextIO) -> None:
    if not stdin.isatty() or not stdout.isatty():
        message = (
            "SQL explorer requires an interactive terminal. "
            "Terminal Python/IPython consoles are supported; notebooks and redirected "
            "input are not."
        )
        raise SqlExplorerEnvironmentError(message)


def _database_choices() -> tuple[tuple[str, str], ...]:
    results = sql.validate_connections(connect=False)
    choices = tuple(
        (result.connection_key, result.backend)
        for result in results
        if result.valid and result.backend is not None
    )
    if not choices:
        message = "No valid SQL connections were found in .connections."
        raise SqlExplorerConfigurationError(message)
    return choices


__all__ = ["run"]
