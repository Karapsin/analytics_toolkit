"""Cursor-adjacent SQL completion menu with tab-scoped lifecycle updates."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, cast

from textual.binding import Binding, BindingType
from textual.widgets import OptionList

if TYPE_CHECKING:
    from .app import SqlExplorerApp


class CompletionMenu(OptionList):
    """Cursor-adjacent SQL completion overlay."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "close", "Close completion", show=False),
        Binding("enter", "accept", "Accept completion", show=False),
    ]

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.suggestions: tuple[str, ...] = ()

    @property
    def is_open(self) -> bool:
        return self.styles.display != "none"

    def on_mount(self) -> None:
        from .workspace import workspace_for  # noqa: PLC0415 -- avoids widget import cycle.

        editor = workspace_for(self).editor
        for attribute in ("scroll_x", "scroll_y", "selection"):
            self.watch(editor, attribute, self._queue_position)

    def on_resize(self) -> None:
        self._queue_position()

    def _queue_position(self) -> None:
        from .workspace import workspace_for  # noqa: PLC0415 -- avoids widget import cycle.

        if self.is_mounted and self.is_open:
            # Widget lifecycle changes must use the app's tab/epoch guarded scheduler.
            app = cast("SqlExplorerApp", self.app)
            app._schedule_completion_position(workspace_for(self))  # noqa: SLF001

    def open(self, suggestions: tuple[str, ...]) -> None:
        self.suggestions = suggestions
        self.clear_options()
        self.add_options(suggestions)
        self.highlighted = 0 if suggestions else None
        self.styles.display = "block" if suggestions else "none"
        self.styles.height = min(12, len(suggestions) + 2)

    def move_highlight(self, offset: int) -> None:
        if not self.suggestions:
            return
        current = self.highlighted if self.highlighted is not None else 0
        self.highlighted = max(0, min(current + offset, len(self.suggestions) - 1))

    def selected_suggestion(self) -> str | None:
        if self.highlighted is None:
            return None
        return self.suggestions[self.highlighted]

    def action_close(self) -> None:
        from .workspace import workspace_for  # noqa: PLC0415 -- avoids widget import cycle.

        self.styles.display = "none"
        self.suggestions = ()
        workspace = workspace_for(self)
        workspace.completion_allow_empty_columns = False
        cast("SqlExplorerApp", self.app)._cancel_completion_request(workspace)  # noqa: SLF001
        workspace.editor.focus()

    def action_accept(self) -> None:
        cast("SqlExplorerApp", self.app).action_complete()
