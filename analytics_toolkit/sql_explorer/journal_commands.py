"""Open journal entries as independent, unsaved editor tabs."""

# ruff: noqa: SLF001 -- app mixin.

from __future__ import annotations

from typing import Any, cast

from .errors import SqlExplorerConfigurationError
from .journal_screen import JournalScreen


class SqlExplorerJournalCommandsMixin:
    def _command_journal(self, arguments: list[str]) -> None:
        app = cast("Any", self)
        if arguments:
            app.show_error(SqlExplorerConfigurationError("Usage: journal"))
            return
        self.action_open_journal()

    def action_open_journal(self) -> None:
        app = cast("Any", self)
        if isinstance(app.screen, JournalScreen):
            app.screen.action_close()
            return
        if len(app.screen_stack) > 1 or app._exit_requested:
            return
        session = app.active_workspace.session
        journal = session.journal
        database = session.database

        def opened(sql: str | None) -> None:
            if sql is None:
                return
            child = session.fork()
            child.database = database
            target = app._add_workspace(child)

            def populate() -> None:
                target.editor.load_text(sql)
                target.saved_text = ""
                app._activate_tab(target.tab_id)
                app._refresh_tab(target)
                target.editor.focus()

            target.pending_mount_action = populate

        app.push_screen(JournalScreen(journal, database.connection_key), opened)
