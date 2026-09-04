"""Tab-scoped completion commands backed by per-database metadata queues."""

# ruff: noqa: SLF001

from __future__ import annotations

from contextlib import suppress
from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast

from .completion import (
    MIN_TABLE_PREFIX_LENGTH,
    CompletionContext,
    CompletionResult,
    filter_suggestions,
    keyword_suggestions,
    parse_completion_context,
)

if TYPE_CHECKING:
    from .workspace import SqlExplorerWorkspace


class SqlExplorerCompletionCommandsMixin:
    """Keep completion UI and metadata callbacks attached to their owner tab."""

    def _start_completion_coordinator(
        self,
        workspace: SqlExplorerWorkspace | None = None,
    ) -> None:
        app = cast("Any", self)
        workspace = workspace or app.active_workspace
        database = workspace.session.database
        workspace.completion = app._completion_pool.acquire(
            database.connection_key,
            database.backend,
            workspace.tab_id,
            on_error=lambda result, exc: app._metadata_error_from_thread(
                workspace.tab_id,
                result,
                exc,
            ),
        )

    def _completion_at_cursor(
        self,
        workspace: SqlExplorerWorkspace | None = None,
    ) -> CompletionContext:
        app = cast("Any", self)
        workspace = workspace or app.active_workspace
        editor = workspace.editor
        row, column = editor.cursor_location
        lines = editor.text.splitlines(keepends=True)
        offset = sum(len(line) for line in lines[:row]) + column
        coordinator = workspace.completion
        catalogs = coordinator.known_catalogs() if coordinator is not None else None
        return parse_completion_context(
            editor.text,
            offset,
            backend=workspace.session.database.backend,
            connection_key=workspace.session.database.connection_key,
            trino_catalogs=catalogs,
        )

    def _request_completion(
        self,
        workspace: SqlExplorerWorkspace | None = None,
    ) -> bool:
        app = cast("Any", self)
        workspace = workspace or app.active_workspace
        editor = workspace.editor
        if not editor.selection.is_empty:
            return False
        context = app._completion_at_cursor(workspace)
        if context.request.kind == "keyword":
            suggestions = keyword_suggestions(context.request.prefix)
            handled = bool(context.request.prefix and suggestions)
            if handled:
                app._open_completion(context, suggestions, workspace=workspace)
            return handled

        coordinator = workspace.completion
        if coordinator is None:
            return False
        workspace.completion_context = context
        opened = app._open_namespace_completion(context, workspace)
        cached = coordinator.cached(context.request)
        if cached is not None:
            if cached:
                app._open_completion(context, cached, workspace=workspace)
            return True
        if len(context.request.prefix) < MIN_TABLE_PREFIX_LENGTH:
            if not opened:
                app._set_notice(
                    f"Type at least {MIN_TABLE_PREFIX_LENGTH} table-name characters.",
                    workspace,
                )
            return bool(opened)
        tab_id = workspace.tab_id
        coordinator.enqueue(
            replace(
                context.request,
                prefix=context.request.prefix[:MIN_TABLE_PREFIX_LENGTH],
            ),
            on_success=lambda result: app._completion_from_thread(tab_id, result),
            on_error=lambda result, exc: app._metadata_error_from_thread(
                tab_id,
                result,
                exc,
            ),
            owner_id=tab_id,
        )
        app._set_notice("Loading matching table names...", workspace)
        return True

    def _open_namespace_completion(
        self,
        context: CompletionContext,
        workspace: SqlExplorerWorkspace | None = None,
    ) -> bool:
        app = cast("Any", self)
        workspace = workspace or app.active_workspace
        coordinator = workspace.completion
        if coordinator is None:
            return False
        request = context.request
        values: tuple[str, ...] | None
        if request.backend == "trino" and request.catalog is None:
            values = coordinator.known_catalogs()
        elif request.backend == "trino" and request.schema is None:
            values = coordinator.cached_schemas(request.catalog)
        elif request.schema is None:
            values = coordinator.cached_schemas(None)
            if values is None:
                tab_id = workspace.tab_id
                coordinator.enqueue_schemas(
                    on_success=lambda result: app._namespace_from_thread(tab_id, result),
                    on_error=lambda result, exc: app._metadata_error_from_thread(
                        tab_id,
                        result,
                        exc,
                    ),
                    owner_id=tab_id,
                )
        else:
            values = None
        suggestions = filter_suggestions(values or (), request.prefix)
        if suggestions:
            app._open_completion(context, suggestions, workspace=workspace)
            return True
        return values is None

    def _completion_from_thread(self, tab_id: str, result: CompletionResult) -> None:
        app = cast("Any", self)
        with suppress(RuntimeError):
            app.call_from_thread(app._receive_completion, result, tab_id)

    def _namespace_from_thread(self, tab_id: str, result: CompletionResult) -> None:
        app = cast("Any", self)
        with suppress(RuntimeError):
            app.call_from_thread(app._receive_namespace, result, tab_id)

    def _metadata_error_from_thread(
        self,
        tab_id: str,
        _result: CompletionResult,
        exc: Exception,
    ) -> None:
        app = cast("Any", self)
        with suppress(RuntimeError):
            app.call_from_thread(app._receive_metadata_error, tab_id, exc)

    def _receive_metadata_error(self, tab_id: str, exc: Exception) -> None:
        app = cast("Any", self)
        workspace = app._workspaces.get(tab_id)
        if workspace is not None:
            app._set_notice(
                f"Metadata completion unavailable: {type(exc).__name__}: {exc}",
                workspace,
            )

    def _receive_completion(
        self,
        result: CompletionResult,
        tab_id: str | None = None,
    ) -> None:
        app = cast("Any", self)
        workspace = app._workspaces.get(tab_id or app._active_tab_id)
        if workspace is None or len(app.screen_stack) > 1:
            return
        context = app._completion_at_cursor(workspace)
        if context.request.scope != result.request.scope:
            return
        coordinator = workspace.completion
        suggestions = coordinator.cached(context.request) if coordinator else None
        if suggestions:
            app._open_completion(context, suggestions, workspace=workspace)
        else:
            workspace.completion_menu.action_close()
            app._set_notice("No matching table names found.", workspace)

    def _receive_namespace(
        self,
        _result: CompletionResult,
        tab_id: str | None = None,
    ) -> None:
        app = cast("Any", self)
        workspace = app._workspaces.get(tab_id or app._active_tab_id)
        if workspace is None or len(app.screen_stack) > 1:
            return
        context = app._completion_at_cursor(workspace)
        if context.request.kind == "table":
            app._open_namespace_completion(context, workspace)

    def _open_completion(
        self,
        context: CompletionContext,
        suggestions: tuple[str, ...],
        *,
        accept_single: bool = True,
        workspace: SqlExplorerWorkspace | None = None,
    ) -> None:
        app = cast("Any", self)
        workspace = workspace or app.active_workspace
        workspace.completion_context = context
        menu = workspace.completion_menu
        if accept_single and len(suggestions) == 1:
            app._insert_completion(context, suggestions[0], workspace)
            menu.action_close()
            return
        editor = workspace.editor
        cursor_x, cursor_y = editor.cursor_render_offset
        menu.styles.offset = (cursor_x + editor.gutter_width + 1, cursor_y + 1)
        menu.open(suggestions)

    def _accept_completion(
        self,
        workspace: SqlExplorerWorkspace | None = None,
    ) -> None:
        app = cast("Any", self)
        workspace = workspace or app.active_workspace
        menu = workspace.completion_menu
        suggestion = menu.selected_suggestion()
        if suggestion is None:
            menu.action_close()
            return
        context = app._completion_at_cursor(workspace)
        app._insert_completion(context, suggestion, workspace)
        menu.action_close()

    def _insert_completion(
        self,
        context: CompletionContext,
        suggestion: str,
        workspace: SqlExplorerWorkspace | None = None,
    ) -> None:
        app = cast("Any", self)
        workspace = workspace or app.active_workspace
        editor = workspace.editor
        start = app._offset_to_location(editor.text, context.replacement_start)
        end = app._offset_to_location(editor.text, context.replacement_end)
        result = editor.replace(suggestion, start, end, maintain_selection_offset=False)
        editor.cursor_location = result.end_location

    def _refresh_open_completion(
        self,
        workspace: SqlExplorerWorkspace | None = None,
    ) -> None:
        app = cast("Any", self)
        workspace = workspace or app.active_workspace
        menu = workspace.completion_menu
        if not menu.is_open:
            return
        previous = workspace.completion_context
        context = app._completion_at_cursor(workspace)
        if previous is None or previous.request.scope != context.request.scope:
            menu.action_close()
            return
        suggestions: tuple[str, ...] | None
        if context.request.kind == "keyword":
            suggestions = keyword_suggestions(context.request.prefix)
        else:
            coordinator = workspace.completion
            suggestions = coordinator.cached(context.request) if coordinator else None
            if suggestions is None:
                return
        if suggestions:
            app._open_completion(
                context,
                suggestions,
                accept_single=False,
                workspace=workspace,
            )
        else:
            menu.action_close()

    @staticmethod
    def _offset_to_location(text: str, offset: int) -> tuple[int, int]:
        before = text[:offset]
        row = before.count("\n")
        column = len(before.rsplit("\n", 1)[-1])
        return row, column


__all__ = ["SqlExplorerCompletionCommandsMixin"]
