"""Tab-scoped completion commands backed by per-database metadata queues."""

# ruff: noqa: SLF001

from __future__ import annotations

from contextlib import suppress
from dataclasses import replace
from threading import get_ident
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
            allow_empty_column_prefix=workspace.completion_allow_empty_columns,
        )

    def _request_completion(
        self,
        workspace: SqlExplorerWorkspace | None = None,
        *,
        columns_only: bool = False,
    ) -> bool:
        app = cast("Any", self)
        workspace = workspace or app.active_workspace
        editor = workspace.editor
        workspace.completion_allow_empty_columns = columns_only
        context = (
            app._completion_at_cursor(workspace)
            if editor.cursor_count == 1 and editor.selection.is_empty
            else None
        )
        if context is None or (columns_only and context.request.kind != "column"):
            app._cancel_completion_request(workspace)
            workspace.completion_allow_empty_columns = False
            return False
        workspace.completion_requested_text = editor.text
        workspace.completion_cursor = editor.cursor_location
        workspace.completion_candidates = ()
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
        opened = (
            app._open_namespace_completion(context, workspace)
            if context.request.kind == "table"
            else False
        )
        cached = coordinator.cached(context.request)
        if cached is not None:
            if cached:
                workspace.completion_candidates = ()
                app._open_completion(context, cached, workspace=workspace)
            return True
        if (
            context.request.kind == "table"
            and len(context.request.prefix) < MIN_TABLE_PREFIX_LENGTH
        ):
            if not opened:
                app._set_notice(
                    f"Type at least {MIN_TABLE_PREFIX_LENGTH} table-name characters.",
                    workspace,
                )
            return bool(opened)
        tab_id = workspace.tab_id
        epoch = workspace.completion_epoch
        notice = f"Loading matching {context.request.kind} names..."
        workspace.completion_loading_notice = notice
        app._set_notice(notice, workspace)
        coordinator.enqueue(
            replace(
                context.request,
                prefix=context.request.prefix[:MIN_TABLE_PREFIX_LENGTH]
                if context.request.kind == "table"
                else "",
            ),
            on_success=lambda result: app._completion_from_thread(tab_id, result, epoch),
            on_error=lambda result, exc: app._metadata_error_from_thread(
                tab_id,
                result,
                exc,
                epoch,
            ),
            owner_id=tab_id,
        )
        return True

    def _open_namespace_completion(
        self,
        context: CompletionContext,
        workspace: SqlExplorerWorkspace | None = None,
        *,
        accept_single: bool = True,
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
                epoch = workspace.completion_epoch
                coordinator.enqueue_schemas(
                    on_success=lambda result: app._namespace_from_thread(tab_id, result, epoch),
                    on_error=lambda result, exc: app._metadata_error_from_thread(
                        tab_id,
                        result,
                        exc,
                        epoch,
                    ),
                    owner_id=tab_id,
                )
        else:
            values = None
        suggestions = filter_suggestions(values or (), request.prefix)
        if suggestions:
            workspace.completion_candidates = values or ()
            app._open_completion(
                context, suggestions, workspace=workspace, accept_single=accept_single
            )
            return True
        return values is None

    def _completion_from_thread(
        self, tab_id: str, result: CompletionResult, epoch: int | None = None
    ) -> None:
        app = cast("Any", self)
        app._post_metadata(app._receive_completion, result, tab_id, epoch)

    def _namespace_from_thread(
        self, tab_id: str, result: CompletionResult, epoch: int | None = None
    ) -> None:
        app = cast("Any", self)
        app._post_metadata(app._receive_namespace, result, tab_id, epoch)

    def _post_metadata(self, callback: Any, *args: Any) -> None:
        app = cast("Any", self)
        if get_ident() == app._thread_id:
            callback(*args)
        else:
            with suppress(RuntimeError):
                app.call_from_thread(callback, *args)

    def _metadata_error_from_thread(
        self,
        tab_id: str,
        _result: CompletionResult,
        exc: Exception,
        epoch: int | None = None,
    ) -> None:
        app = cast("Any", self)
        app._post_metadata(app._receive_metadata_error, tab_id, exc, epoch)

    def _receive_metadata_error(
        self, tab_id: str, exc: Exception, epoch: int | None = None
    ) -> None:
        app = cast("Any", self)
        workspace = app._workspaces.get(tab_id)
        if workspace is not None and (epoch is None or epoch == workspace.completion_epoch):
            workspace.completion_loading_notice = None
            app._set_notice(
                f"Metadata completion unavailable: {type(exc).__name__}: {exc}",
                workspace,
            )

    def _receive_completion(
        self,
        result: CompletionResult,
        tab_id: str | None = None,
        epoch: int | None = None,
    ) -> None:
        app = cast("Any", self)
        workspace = app._workspaces.get(tab_id or app._active_tab_id)
        if workspace is None or len(app.screen_stack) > 1:
            return
        if (
            workspace.completion_requested_text is None
            or workspace.editor.cursor_count > 1
            or workspace.completion_cursor != workspace.editor.cursor_location
            or workspace.completion_requested_text != workspace.editor.text
            or (epoch is not None and epoch != workspace.completion_epoch)
        ):
            return
        context = app._completion_at_cursor(workspace)
        if context.request.scope != result.request.scope:
            return
        app._finish_completion_loading(workspace)
        coordinator = workspace.completion
        suggestions = coordinator.cached(context.request) if coordinator else None
        if suggestions:
            workspace.completion_candidates = ()
            app._open_completion(
                context,
                suggestions,
                workspace=workspace,
                accept_single=workspace.completion_requested_text == workspace.editor.text,
            )
        else:
            workspace.completion_menu.action_close()
            app._set_notice(f"No matching {context.request.kind} names found.", workspace)

    def _receive_namespace(
        self,
        result: CompletionResult,
        tab_id: str | None = None,
        epoch: int | None = None,
    ) -> None:
        app = cast("Any", self)
        workspace = app._workspaces.get(tab_id or app._active_tab_id)
        if workspace is None or len(app.screen_stack) > 1:
            return
        if (
            workspace.completion_requested_text is None
            or workspace.editor.cursor_count > 1
            or workspace.completion_cursor != workspace.editor.cursor_location
            or workspace.completion_requested_text != workspace.editor.text
            or (epoch is not None and epoch != workspace.completion_epoch)
        ):
            return
        context = app._completion_at_cursor(workspace)
        previous = workspace.completion_context
        if (
            context.request.kind == "table"
            and previous is not None
            and previous.request.scope == context.request.scope
            and result.request.connection_key == context.request.connection_key
        ):
            app._open_namespace_completion(
                context,
                workspace,
                accept_single=workspace.completion_requested_text == workspace.editor.text,
            )

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
        if workspace.editor.cursor_count > 1:
            menu.action_close()
            return
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
        editor = workspace.editor
        if (
            workspace.completion_cursor != editor.cursor_location
            or workspace.completion_requested_text != editor.text
            or editor.cursor_count > 1
        ):
            app._cancel_completion_request(workspace)
        if editor.cursor_count > 1 or not editor.selection.is_empty:
            menu.styles.display = "none"
            return
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
        elif workspace.completion_candidates:
            suggestions = filter_suggestions(
                workspace.completion_candidates, context.request.prefix
            )
        else:
            coordinator = workspace.completion
            suggestions = coordinator.cached(context.request) if coordinator else None
            if suggestions is None:
                suggestions = filter_suggestions(
                    workspace.completion_candidates or menu.suggestions, context.request.prefix
                )
        if suggestions:
            app._open_completion(
                context,
                suggestions,
                accept_single=False,
                workspace=workspace,
            )
        else:
            menu.action_close()

    def _cancel_completion_request(self, workspace: SqlExplorerWorkspace) -> None:
        app = cast("Any", self)
        app._finish_completion_loading(workspace)
        workspace.completion_requested_text = None
        workspace.completion_cursor = None
        workspace.completion_epoch += 1
        if workspace.completion is not None:
            workspace.completion.remove_owner(workspace.tab_id)

    def _finish_completion_loading(self, workspace: SqlExplorerWorkspace) -> None:
        app = cast("Any", self)
        if workspace.completion_loading_notice is not None:
            workspace.completion_loading_notice = None
            app._set_notice("", workspace)

    @staticmethod
    def _offset_to_location(text: str, offset: int) -> tuple[int, int]:
        before = text[:offset]
        row = before.count("\n")
        column = len(before.rsplit("\n", 1)[-1])
        return row, column


__all__ = ["SqlExplorerCompletionCommandsMixin"]
