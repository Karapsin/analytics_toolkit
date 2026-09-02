from __future__ import annotations

import sys
from typing import TextIO

from analytics_toolkit import sql

from .errors import (
    SqlExplorerConfigurationError,
    SqlExplorerDependencyError,
    SqlExplorerEnvironmentError,
)
from .runtime import ExplorerSession


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
        if db_key is None:
            choices = _database_choices()
            db_key = DatabasePickerApp(choices).run()
            if db_key is None:
                return
        from .terminal_keys import install_terminal_key_compatibility  # noqa: PLC0415

        install_terminal_key_compatibility()
        session = ExplorerSession(db_key)
        SqlExplorerApp(session).run()
    finally:
        sql.set_time_print_sink(previous_sink)


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
