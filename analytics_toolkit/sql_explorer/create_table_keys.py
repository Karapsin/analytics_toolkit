"""Arrow-and-Enter navigation for every table-creation control."""

# ruff: noqa: SLF001 -- mixin shares its owning screen's completion state.

from __future__ import annotations

from typing import Any, cast

from textual.widgets import Button, Checkbox, Collapsible, Input, OptionList, Select, TextArea


class CreateTableKeyboardMixin:
    def action_form_vertical(self, direction: int) -> None:
        screen = cast("Any", self)
        focused = screen.app.focused
        if isinstance(focused, OptionList):
            if direction < 0:
                focused.action_cursor_up()
            else:
                focused.action_cursor_down()
        elif (
            isinstance(focused, Input)
            and focused.has_class("create-column-type")
            and screen.type_matches
        ):
            screen.type_index = (screen.type_index + direction) % len(screen.type_matches)
            screen._show_types()
        elif isinstance(focused, TextArea) and (
            (direction < 0 and focused.cursor_location[0] > 0)
            or (direction > 0 and focused.cursor_location[0] < focused.document.line_count - 1)
        ):
            if direction < 0:
                focused.action_cursor_up()
            else:
                focused.action_cursor_down()
        elif direction < 0:
            screen.focus_previous()
        else:
            screen.focus_next()

    def action_form_horizontal(self, direction: int) -> None:
        screen = cast("Any", self)
        focused = screen.app.focused
        if isinstance(focused, (Input, TextArea)):
            if direction < 0:
                focused.action_cursor_left()
            else:
                focused.action_cursor_right()
        elif isinstance(focused, Checkbox):
            focused.value = direction > 0
        elif isinstance(focused, Select):
            values = (
                ("table_schema", "from_sql")
                if focused.id == "create-table-source"
                else ("", "True", "False")
            )
            focused.value = values[(values.index(str(focused.value)) + direction) % len(values)]
        else:
            screen.action_form_vertical(direction)

    def action_form_enter(self) -> None:
        screen = cast("Any", self)
        focused = screen.app.focused
        if screen.accept_type():
            return
        if isinstance(focused, Button):
            focused.press()
        elif isinstance(focused, Checkbox):
            focused.value = not focused.value
        elif isinstance(focused, Select):
            focused.action_show_overlay()
        elif isinstance(focused, OptionList):
            focused.action_select()
        elif isinstance(focused, TextArea):
            result = focused.replace(
                "\n",
                focused.selection.start,
                focused.selection.end,
                maintain_selection_offset=False,
            )
            focused.cursor_location = result.end_location
        elif focused is not None and isinstance(focused.parent, Collapsible):
            focused.parent.collapsed = not focused.parent.collapsed
        else:
            screen.focus_next()

    def action_form_indent(self, direction: int) -> None:
        screen = cast("Any", self)
        focused = screen.app.focused
        if not isinstance(focused, TextArea):
            return
        row, column = focused.cursor_location
        if direction > 0:
            result = focused.replace(
                "    ",
                focused.selection.start,
                focused.selection.end,
                maintain_selection_offset=False,
            )
        else:
            line = focused.document.get_line(row)
            width = min(4, len(line) - len(line.lstrip(" ")))
            result = focused.replace("", (row, 0), (row, width), maintain_selection_offset=False)
            focused.cursor_location = (row, max(0, column - width))
            return
        focused.cursor_location = result.end_location
