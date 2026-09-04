"""Tab-strip controls and tab lifecycle confirmation screens."""

# ruff: noqa: FBT001, FBT003, RUF001, SLF001

from __future__ import annotations

from contextlib import suppress
from typing import TYPE_CHECKING, Any, ClassVar, Literal, cast

from textual.binding import Binding, BindingType
from textual.containers import Horizontal, HorizontalScroll, Vertical
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from .workspace import SqlExplorerWorkspace

if TYPE_CHECKING:
    from textual.app import ComposeResult

    from .runtime import ExplorerSession

CloseDecision = Literal["save", "discard", "cancel"]
_TAB_SWITCH_MINIMUM = 2


class TabSelectButton(Button):
    def __init__(self, tab_id: str, label: str) -> None:
        super().__init__(label, classes="tab-select")
        self.tab_id = tab_id


class TabCloseButton(Button):
    def __init__(self, tab_id: str) -> None:
        super().__init__("×", classes="tab-close")
        self.tab_id = tab_id
        self.tooltip = "Close tab"


class NewTabButton(Button):
    def __init__(self) -> None:
        super().__init__("+", id="new-tab", classes="new-tab")
        self.tooltip = "New tab"


class WorkspaceTab(Horizontal):
    def __init__(self, tab_id: str, title: str) -> None:
        super().__init__(id=f"tab-{tab_id}", classes="workspace-tab")
        self.tab_id = tab_id
        self._title = title

    def compose(self) -> ComposeResult:
        yield TabSelectButton(self.tab_id, self._title)
        yield TabCloseButton(self.tab_id)

    def set_title(self, title: str, *, path: str | None = None) -> None:
        self._title = title
        button = self.query_one(TabSelectButton)
        button.label = title
        button.tooltip = path or title

    def set_active(self, active: bool) -> None:
        self.set_class(active, "active")

    def set_close_enabled(self, enabled: bool) -> None:
        self.query_one(TabCloseButton).disabled = not enabled


class SaveChangesScreen(ModalScreen[CloseDecision]):
    """Choose whether a dirty workspace is saved before it closes."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("left", "focus_previous", "Previous choice", show=False, priority=True),
        Binding("right", "focus_next", "Next choice", show=False, priority=True),
        Binding("s", "save", "Save", show=False),
        Binding("d", "discard", "Don't save", show=False),
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    CSS = """
    SaveChangesScreen {
        align: center middle;
    }
    #save-changes-dialog {
        width: 70%;
        max-width: 80;
        height: auto;
        border: round $warning;
        background: $panel;
        padding: 1 2;
    }
    #save-changes-buttons {
        height: 3;
        align-horizontal: center;
    }
    """

    def __init__(self, title: str) -> None:
        super().__init__()
        self.title = title

    def compose(self) -> ComposeResult:
        with Vertical(id="save-changes-dialog"):
            yield Static(f"Save changes to {self.title}?", markup=False)
            with Horizontal(id="save-changes-buttons"):
                yield Button("Save [S]", variant="primary", id="save-changes-save")
                yield Button("Don't Save [D]", variant="warning", id="save-changes-discard")
                yield Button("Cancel", id="save-changes-cancel")

    def on_mount(self) -> None:
        self.query_one("#save-changes-save", Button).focus()

    def action_focus_previous(self) -> None:
        self.focus_previous()

    def action_focus_next(self) -> None:
        self.focus_next()

    def action_save(self) -> None:
        self.dismiss("save")

    def action_discard(self) -> None:
        self.dismiss("discard")

    def action_cancel(self) -> None:
        self.dismiss("cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        decisions: dict[str | None, CloseDecision] = {
            "save-changes-save": "save",
            "save-changes-discard": "discard",
            "save-changes-cancel": "cancel",
        }
        decision = decisions.get(event.button.id)
        if decision is not None:
            self.dismiss(decision)


class ConfirmConcurrencyScreen(ModalScreen[bool]):
    """Confirm an unusually high user-query concurrency limit."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("left", "select_confirm", "Select confirm", show=False, priority=True),
        Binding("right", "select_cancel", "Select cancel", show=False, priority=True),
        Binding("y", "confirm", "Confirm", show=False),
        Binding("n,escape", "cancel", "Cancel", show=False),
    ]

    CSS = """
    ConfirmConcurrencyScreen {
        align: center middle;
    }
    #concurrency-dialog {
        width: 70%;
        max-width: 80;
        height: auto;
        border: round $warning;
        background: $panel;
        padding: 1 2;
    }
    #concurrency-buttons {
        height: 3;
        align-horizontal: center;
    }
    """

    def __init__(self, value: int) -> None:
        super().__init__()
        self.value = value

    def compose(self) -> ComposeResult:
        with Vertical(id="concurrency-dialog"):
            yield Static(
                f"Allow up to {self.value} concurrent user queries? "
                "This can overload database services.",
                markup=False,
            )
            with Horizontal(id="concurrency-buttons"):
                yield Button("Apply [Y]", variant="warning", id="concurrency-confirm")
                yield Button("Cancel [N]", id="concurrency-cancel")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)

    def action_select_confirm(self) -> None:
        self.query_one("#concurrency-confirm", Button).focus()

    def action_select_cancel(self) -> None:
        self.query_one("#concurrency-cancel", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "concurrency-confirm")


class SqlExplorerTabCommandsMixin:
    """Create, activate, switch, and safely close SQL Explorer workspaces."""

    def _fork_session(self, source: ExplorerSession) -> ExplorerSession:
        return source.fork()

    def action_new_tab(self) -> None:
        app = cast("Any", self)
        if len(app.screen_stack) == 1:
            app._add_workspace()

    def _add_workspace(
        self,
        session: ExplorerSession | None = None,
    ) -> SqlExplorerWorkspace:
        app = cast("Any", self)
        source = app.active_workspace
        app._workspace_sequence += 1
        app._untitled_sequence += 1
        tab_id = str(app._workspace_sequence)
        workspace = SqlExplorerWorkspace(
            tab_id,
            app._untitled_sequence,
            session or app._fork_session(source.session),
        )
        workspace.set_visible(False)
        app._workspaces[tab_id] = workspace
        app._tab_order.append(tab_id)
        tab = WorkspaceTab(tab_id, workspace.tab_title)
        app.query_one("#tab-strip", HorizontalScroll).mount(
            tab,
            before=app.query_one(NewTabButton),
        )
        app.query_one("#workspace-stack", Vertical).mount(workspace)
        return workspace

    def _finish_added_workspace(self, tab_id: str) -> None:
        app = cast("Any", self)
        workspace = app._workspaces.get(tab_id)
        if workspace is None:
            return
        workspace.result_message.styles.display = "none"
        workspace.completion_menu.styles.display = "none"
        app._start_completion_coordinator(workspace)
        app._activate_tab(tab_id)
        app._refresh_close_buttons()

    def _activate_tab(self, tab_id: str) -> None:
        app = cast("Any", self)
        if tab_id not in app._workspaces:
            return
        current = app._workspaces.get(app._active_tab_id)
        if current is not None:
            current.remember_focus(app.focused)
            if current.find_bar.is_open:
                app.disable_find_navigation()
        app._active_tab_id = tab_id
        for candidate_id, workspace in app._workspaces.items():
            active = candidate_id == tab_id
            workspace.set_visible(active)
            with suppress(NoMatches):
                app.query_one(f"#tab-{candidate_id}", WorkspaceTab).set_active(active)
        if app.active_workspace.find_bar.is_open:
            app.enable_find_navigation()
        app.call_after_refresh(app._restore_active_focus)

    def _restore_active_focus(self) -> None:
        app = cast("Any", self)
        app.active_workspace.restore_focus()
        app._update_status(app.active_workspace)

    def action_next_tab(self) -> None:
        self._switch_tab(1)

    def action_previous_tab(self) -> None:
        self._switch_tab(-1)

    def _switch_tab(self, direction: int) -> None:
        app = cast("Any", self)
        if len(app.screen_stack) > 1 or len(app._tab_order) < _TAB_SWITCH_MINIMUM:
            return
        index = app._tab_order.index(app._active_tab_id)
        app._activate_tab(app._tab_order[(index + direction) % len(app._tab_order)])

    def action_close_tab(self) -> None:
        app = cast("Any", self)
        if len(app.screen_stack) == 1:
            app._request_close_tab(app._active_tab_id)

    def _request_close_tab(self, tab_id: str) -> None:
        app = cast("Any", self)
        workspace = app._workspaces.get(tab_id)
        if workspace is None:
            return
        if len(app._tab_order) == 1:
            app._set_notice("The final SQL Explorer tab cannot be closed.", workspace)
            return
        app._activate_tab(tab_id)
        if workspace.is_dirty:
            app.push_screen(
                SaveChangesScreen(workspace.file_label),
                lambda decision: app._close_decision(tab_id, decision),
            )
            return
        app._finish_close_request(workspace)

    def _close_decision(self, tab_id: str, decision: object) -> None:
        app = cast("Any", self)
        workspace = app._workspaces.get(tab_id)
        if workspace is None or decision == "cancel":
            return
        if decision == "save":
            if workspace.current_file is not None:
                if app._save_workspace(workspace):
                    app._finish_close_request(workspace)
            else:
                app._start_new_sql_file(
                    workspace,
                    text=workspace.editor.text,
                    after_create=lambda created: (
                        app._finish_close_request(workspace) if created else None
                    ),
                )
            return
        app._finish_close_request(workspace)

    def _finish_close_request(self, workspace: SqlExplorerWorkspace) -> None:
        app = cast("Any", self)
        removed = app._query_scheduler.remove_pending_tab(workspace.tab_id)
        if removed is not None:
            workspace.reset_query_state()
            app._drain_query_queue()
        if workspace.busy:
            workspace.closing = True
            app._request_cancel(workspace=workspace, close_after=True)
            return
        app._remove_workspace(workspace.tab_id)

    def _remove_workspace(self, tab_id: str) -> None:
        app = cast("Any", self)
        if tab_id not in app._workspaces or len(app._tab_order) == 1:
            return
        index = app._tab_order.index(tab_id)
        was_active = tab_id == app._active_tab_id
        workspace = app._workspaces.pop(tab_id)
        app._completion_pool.release(
            workspace.tab_id,
            workspace.session.database.connection_key,
        )
        workspace.completion = None
        app._tab_order.remove(tab_id)
        with suppress(NoMatches):
            app.query_one(f"#tab-{tab_id}", WorkspaceTab).remove()
        workspace.remove()
        if was_active:
            next_index = min(index, len(app._tab_order) - 1)
            app._active_tab_id = app._tab_order[next_index]
            app._activate_tab(app._active_tab_id)
        app._refresh_close_buttons()

    def _refresh_tab(self, workspace: SqlExplorerWorkspace) -> None:
        app = cast("Any", self)
        with suppress(NoMatches):
            tab = app.query_one(f"#tab-{workspace.tab_id}", WorkspaceTab)
            path = str(workspace.current_file) if workspace.current_file is not None else None
            tab.set_title(workspace.tab_title, path=path)

    def _refresh_close_buttons(self) -> None:
        app = cast("Any", self)
        enabled = len(app._tab_order) > 1
        for tab_id in app._tab_order:
            with suppress(NoMatches):
                app.query_one(f"#tab-{tab_id}", WorkspaceTab).set_close_enabled(enabled)


__all__ = [
    "CloseDecision",
    "ConfirmConcurrencyScreen",
    "NewTabButton",
    "SaveChangesScreen",
    "SqlExplorerTabCommandsMixin",
    "TabCloseButton",
    "TabSelectButton",
    "WorkspaceTab",
]
