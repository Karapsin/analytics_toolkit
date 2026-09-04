"""Consistent single-line editing for SQL Explorer text-entry surfaces."""

# ruff: noqa: FBT001, FBT002

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, cast

from textual.binding import Binding, BindingType
from textual.widgets import Input

if TYPE_CHECKING:
    from collections.abc import Callable

    from rich.text import Text
    from textual import events

    from .app import SqlExplorerApp


@dataclass(frozen=True)
class _InputSnapshot:
    value: str
    cursor: int
    anchor: int | None


class EditableInput(Input):
    """A Textual input with portable selection, clipboard, and history keys."""

    COMPONENT_CLASSES: ClassVar[set[str]] = {
        *Input.COMPONENT_CLASSES,
        "input--selection",
    }
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("left", "cursor_left", "Move left", show=False),
        Binding("shift+left", "cursor_left(True)", "Select left", show=False),
        Binding("right", "cursor_right", "Move right", show=False),
        Binding("shift+right", "cursor_right(True)", "Select right", show=False),
        Binding("ctrl+left", "cursor_left_word", "Move left one word", show=False),
        Binding(
            "ctrl+shift+left",
            "cursor_left_word(True)",
            "Select left one word",
            show=False,
        ),
        Binding("ctrl+right", "cursor_right_word", "Move right one word", show=False),
        Binding(
            "ctrl+shift+right",
            "cursor_right_word(True)",
            "Select right one word",
            show=False,
        ),
        Binding("home,ctrl+home", "home", "Move to start", show=False),
        Binding("shift+home", "home(True)", "Select to start", show=False),
        Binding("end,ctrl+end", "end", "Move to end", show=False),
        Binding("shift+end", "end(True)", "Select to end", show=False),
        Binding("ctrl+a", "select_all", "Select all", show=False),
        Binding("ctrl+c", "copy", "Copy", show=False),
        Binding("ctrl+x", "cut", "Cut", show=False),
        Binding("ctrl+v", "paste", "Paste", show=False),
        Binding("ctrl+z", "undo", "Undo", show=False),
        Binding("ctrl+y,ctrl+shift+z", "redo", "Redo", show=False),
        Binding("ctrl+backspace", "delete_left_word", "Delete word left", show=False),
        Binding("ctrl+delete", "delete_right_word", "Delete word right", show=False),
    ]

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._selection_anchor: int | None = None
        self._preserve_cursor_on_focus = False
        self._undo_stack: list[_InputSnapshot] = []
        self._redo_stack: list[_InputSnapshot] = []
        super().__init__(*args, **kwargs)
        self.cursor_blink = False

    @property
    def selection(self) -> tuple[int, int]:
        anchor = self.cursor_position if self._selection_anchor is None else self._selection_anchor
        return min(anchor, self.cursor_position), max(anchor, self.cursor_position)

    @property
    def selected_text(self) -> str:
        start, end = self.selection
        return self.value[start:end]

    @property
    def _value(self) -> Text:
        rendered = super()._value
        start, end = self.selection
        if start != end:
            rendered.stylize(self.get_component_rich_style("input--selection"), start, end)
        return rendered

    def _watch_value(self, value: str) -> None:
        self.cursor_position = min(self.cursor_position, len(value))
        if self._selection_anchor is not None:
            self._selection_anchor = min(self._selection_anchor, len(value))
        super()._watch_value(value)

    def _snapshot(self) -> _InputSnapshot:
        return _InputSnapshot(self.value, self.cursor_position, self._selection_anchor)

    def _restore(self, snapshot: _InputSnapshot) -> None:
        self.value = snapshot.value
        self.cursor_position = snapshot.cursor
        self._selection_anchor = snapshot.anchor
        self.refresh()

    def _record_change(self, before: _InputSnapshot) -> None:
        if self.value != before.value:
            self._undo_stack.append(before)
            del self._undo_stack[:-100]
            self._redo_stack.clear()

    def _delete_selection(self) -> bool:
        start, end = self.selection
        if start == end:
            return False
        self.value = f"{self.value[:start]}{self.value[end:]}"
        self.cursor_position = start
        self._selection_anchor = None
        self.refresh()
        return True

    def _edit(self, action: Callable[[], None]) -> None:
        before = self._snapshot()
        if not self._delete_selection():
            self._selection_anchor = None
            action()
        self._record_change(before)

    def _begin_selection(self) -> None:
        if self._selection_anchor is None:
            self._selection_anchor = self.cursor_position

    def _move(
        self,
        action: Callable[[], None],
        *,
        select: bool,
        collapse_to: str,
    ) -> None:
        start, end = self.selection
        if select:
            self._begin_selection()
            action()
        elif start != end:
            self._selection_anchor = None
            self.cursor_position = start if collapse_to == "start" else end
        else:
            self._selection_anchor = None
            action()
        self.refresh()

    def insert_text_at_cursor(self, text: str) -> None:
        before = self._snapshot()
        had_selection = self._delete_selection()
        self._selection_anchor = None
        value_before_insert = self.value
        super().insert_text_at_cursor(text)
        if had_selection and self.value == value_before_insert:
            self._restore(before)
            return
        self._record_change(before)

    def action_cursor_left(self, select: bool = False) -> None:
        self._move(super().action_cursor_left, select=select, collapse_to="start")

    def action_cursor_right(self, select: bool = False) -> None:
        self._move(super().action_cursor_right, select=select, collapse_to="end")

    def action_cursor_left_word(self, select: bool = False) -> None:
        self._move(super().action_cursor_left_word, select=select, collapse_to="start")

    def action_cursor_right_word(self, select: bool = False) -> None:
        self._move(super().action_cursor_right_word, select=select, collapse_to="end")

    def action_home(self, select: bool = False) -> None:
        if select:
            self._begin_selection()
        else:
            self._selection_anchor = None
        super().action_home()
        self.refresh()

    def action_end(self, select: bool = False) -> None:
        if select:
            self._begin_selection()
        else:
            self._selection_anchor = None
        super().action_end()
        self.refresh()

    def action_select_all(self) -> None:
        self._selection_anchor = 0
        self.cursor_position = len(self.value)
        self.refresh()

    def copy_selection(self) -> bool:
        value = self.selected_text
        if not value:
            return False
        cast("SqlExplorerApp", self.app).copy_to_explorer_clipboard(value)
        return True

    def action_copy(self) -> None:
        self.copy_selection()

    def action_cut(self) -> None:
        if not self.selected_text:
            return
        before = self._snapshot()
        self.copy_selection()
        self._delete_selection()
        self._record_change(before)

    def action_paste(self) -> None:
        value = cast("SqlExplorerApp", self.app).paste_from_explorer_clipboard()
        if value:
            self.insert_text_at_cursor(value.splitlines()[0])

    def action_delete_left(self) -> None:
        self._edit(super().action_delete_left)

    def action_delete_right(self) -> None:
        self._edit(super().action_delete_right)

    def action_delete_left_word(self) -> None:
        self._edit(super().action_delete_left_word)

    def action_delete_right_word(self) -> None:
        self._edit(super().action_delete_right_word)

    def action_delete_left_all(self) -> None:
        self._edit(super().action_delete_left_all)

    def action_delete_right_all(self) -> None:
        self._edit(super().action_delete_right_all)

    def action_undo(self) -> None:
        if not self._undo_stack:
            return
        current = self._snapshot()
        snapshot = self._undo_stack.pop()
        self._redo_stack.append(current)
        self._restore(snapshot)

    def action_redo(self) -> None:
        if not self._redo_stack:
            return
        current = self._snapshot()
        snapshot = self._redo_stack.pop()
        self._undo_stack.append(current)
        self._restore(snapshot)

    def focus_preserving_cursor(self) -> None:
        """Focus the input without Textual moving its cursor to the end."""
        self._preserve_cursor_on_focus = True
        self.focus()
        self.app.call_after_refresh(self._finish_cursor_preserving_focus)

    def _finish_cursor_preserving_focus(self) -> None:
        self._preserve_cursor_on_focus = False

    def _on_paste(self, event: events.Paste) -> None:
        if event.text:
            self.insert_text_at_cursor(event.text.splitlines()[0])
        event.stop()

    def _on_focus(self, event: events.Focus) -> None:
        cursor = self.cursor_position
        anchor = self._selection_anchor
        super()._on_focus(event)
        if self._preserve_cursor_on_focus:
            self.cursor_position = cursor
            self._selection_anchor = anchor
        else:
            self._selection_anchor = None
        self.refresh()

    async def _on_click(self, event: events.Click) -> None:
        anchor = self.cursor_position if event.shift else None
        await super()._on_click(event)
        self._selection_anchor = anchor
        self.refresh()


__all__ = ["EditableInput"]
