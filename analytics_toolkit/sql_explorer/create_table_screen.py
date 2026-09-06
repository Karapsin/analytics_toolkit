"""Compact, tab-owned table creation dialog."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, Dict, Optional

from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Checkbox,
    Collapsible,
    Input,
    OptionList,
    Select,
    Static,
    TextArea,
)

from .create_table import BACKEND_OPTIONS, COMMON_OPTIONS, TYPE_SUGGESTIONS, creation_options
from .create_table_keys import CreateTableKeyboardMixin
from .inputs import EditableInput

if TYPE_CHECKING:
    from textual import events
    from textual.app import ComposeResult


class ColumnRow(Horizontal):
    def __init__(self, name: str = "", data_type: str = "") -> None:
        super().__init__(classes="create-column-row")
        self.initial_name, self.initial_type = name, data_type

    def compose(self) -> ComposeResult:
        yield EditableInput(
            self.initial_name, placeholder="column name", classes="create-column-name"
        )
        yield EditableInput(self.initial_type, placeholder="SQL type", classes="create-column-type")
        yield Button("\u00d7", classes="remove-column", tooltip="Remove column")


class CreateTableScreen(CreateTableKeyboardMixin, ModalScreen[Optional[Dict[str, Any]]]):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Cancel", priority=True),
        Binding("up", "form_vertical(-1)", "Previous field", priority=True),
        Binding("down", "form_vertical(1)", "Next field", priority=True),
        Binding("left", "form_horizontal(-1)", "Previous choice", priority=True),
        Binding("right", "form_horizontal(1)", "Next choice", priority=True),
        Binding("enter", "form_enter", "Choose", priority=True),
    ]
    DEFAULT_CSS = """
    CreateTableScreen { align: center middle; background: $background 75%; }
    #create-table-dialog {
        width: 86; max-width: 95%; height: auto; max-height: 95%;
        border: solid $accent; background: $panel; padding: 1 2;
    }
    #create-table-fields { height: auto; max-height: 28; }
    #create-table-title, #create-table-notice { height: auto; }
    #create-table-name, #create-table-source { height: 3; }
    #create-table-schema { height: auto; }
    .create-column-row { height: 3; }
    .create-column-name { width: 2fr; }
    .create-column-type { width: 3fr; }
    .remove-column {
        width: 3; min-width: 3; height: 1; min-height: 1;
        border: none; padding: 0; margin-top: 1;
    }
    #create-from-sql { height: 7; border: solid $panel-lighten-2; }
    #create-basic-flags { height: auto; }
    #create-basic-flags Checkbox { height: 1; padding: 0; border: none; }
    #create-type-options { height: auto; max-height: 5; border: none; color: $accent; }
    #create-table-actions { height: 3; align-horizontal: right; margin-top: 1; }
    #create-table-actions Button { margin-left: 1; }
    .create-advanced-field { height: 3; }
    .create-advanced-label { height: 1; }
    """

    def __init__(self, db_key: str, backend: str, draft: dict[str, Any]) -> None:
        super().__init__()
        self.db_key, self.backend, self.draft = db_key, backend, draft
        self.type_matches: tuple[str, ...] = ()
        self.type_index = 0
        self.type_input: Input | None = None
        self.accepted_type: str | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="create-table-dialog"):
            yield Static(f"Create table · {self.db_key}", id="create-table-title", markup=False)
            with VerticalScroll(id="create-table-fields"):
                yield EditableInput(
                    str(self.draft.get("table_name", "")),
                    placeholder="table_name",
                    id="create-table-name",
                )
                yield Select(
                    (("table_schema", "table_schema"), ("from_sql", "from_sql")),
                    value=self.draft.get("source", "table_schema"),
                    allow_blank=False,
                    id="create-table-source",
                )
                with Vertical(id="create-table-schema"):
                    for name, data_type in self.draft.get("rows", [("", "")]):
                        yield ColumnRow(name, data_type)
                yield OptionList(id="create-type-options", wrap=False)
                yield Button("+ column", id="create-add-column")
                yield TextArea(str(self.draft.get("from_sql", "")), id="create-from-sql")
                with Vertical(id="create-basic-flags"):
                    for name in ("insert_data", "skip_if_exists", "drop_if_exists"):
                        yield Checkbox(
                            f"{name}: {bool(self.draft.get(name, False))}",
                            bool(self.draft.get(name, False)),
                            id=f"create-{name}",
                        )
                with Collapsible(title="Advanced", collapsed=True, id="create-table-advanced"):
                    for key, kind in (
                        ("source_db", "text"),
                        *COMMON_OPTIONS,
                        *BACKEND_OPTIONS.get(self.backend, ()),
                    ):
                        yield Static(key, classes="create-advanced-label", name=key)
                        value = str(self.draft.get("advanced", {}).get(key, ""))
                        if kind == "bool":
                            yield Select(
                                (("Default", ""), ("True", "True"), ("False", "False")),
                                value=value,
                                allow_blank=False,
                                name=key,
                                classes="create-advanced-field",
                            )
                        else:
                            yield EditableInput(
                                value,
                                placeholder="JSON object" if kind == "json" else "default",
                                name=key,
                                classes="create-advanced-field",
                                type="integer"
                                if kind == "int"
                                else "number"
                                if kind == "float"
                                else "text",
                            )
            yield Static("", id="create-table-notice", markup=False)
            with Horizontal(id="create-table-actions"):
                yield Button("Cancel", id="create-table-cancel")
                yield Button("Create", id="create-table-submit")

    def on_mount(self) -> None:
        self._update_source()
        self.query_one("#create-table-name", Input).focus()

    def _update_source(self) -> None:
        sql_mode = self.query_one("#create-table-source", Select).value == "from_sql"
        self.query_one("#create-table-schema").display = not sql_mode
        self.query_one("#create-add-column").display = not sql_mode
        self.query_one("#create-type-options").display = not sql_mode
        self.query_one("#create-from-sql").display = sql_mode
        self.query_one("#create-insert_data", Checkbox).disabled = not sql_mode
        for widget in self.query(".create-advanced-label, .create-advanced-field"):
            if widget.name == "source_db":
                widget.display = sql_mode

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "create-table-source":
            self._update_source()

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        name = (event.checkbox.id or "")[len("create-") :]
        event.checkbox.label = f"{name}: {event.value}"
        opposites = {
            "create-skip_if_exists": "create-drop_if_exists",
            "create-drop_if_exists": "create-skip_if_exists",
        }
        if event.value and event.checkbox.id in opposites:
            self.query_one(f"#{opposites[event.checkbox.id]}", Checkbox).value = False

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.has_class("create-column-type"):
            self.type_input = event.input
            if event.value == self.accepted_type:
                self.accepted_type = None
                return
            self.type_matches = tuple(
                t
                for t in TYPE_SUGGESTIONS[self.backend]
                if t.casefold().startswith(event.value.casefold())
            )
            self.type_index = 0
            self._show_types()

    def _show_types(self) -> None:
        menu = self.query_one("#create-type-options", OptionList)
        menu.can_focus = False
        menu.clear_options()
        menu.add_options(self.type_matches)
        menu.highlighted = self.type_index if self.type_matches else None
        menu.display = bool(self.type_matches)

    def on_descendant_focus(self, event: events.DescendantFocus) -> None:
        if isinstance(event.widget, Input) and event.widget.has_class("create-column-type"):
            self.on_input_changed(Input.Changed(event.widget, event.widget.value))
        elif isinstance(event.widget, (Input, Select, Checkbox, Button, TextArea)) or isinstance(
            event.widget.parent, Collapsible
        ):
            self.type_matches = ()
            self._show_types()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list.id == "create-type-options" and self.type_input is not None:
            event.stop()
            self._apply_type(self.type_input, event.option_index)
            self.type_input.focus()

    def accept_type(self) -> bool:
        focused = self.app.focused
        if (
            isinstance(focused, Input)
            and focused.has_class("create-column-type")
            and self.type_matches
        ):
            self._apply_type(focused, self.type_index)
            return True
        return False

    def _apply_type(self, field: Input, index: int) -> None:
        self.accepted_type = self.type_matches[index]
        field.value = self.accepted_type
        field.cursor_position = len(field.value)
        self.type_matches = ()
        self._show_types()

    def values(self) -> dict[str, Any]:
        advanced = {
            str(widget.name): str(widget.value)
            for widget in self.query(".create-advanced-field")
            if isinstance(widget, (Input, Select))
        }
        return {
            "table_name": self.query_one("#create-table-name", Input).value,
            "source": self.query_one("#create-table-source", Select).value,
            "rows": [
                (
                    row.query_one(".create-column-name", Input).value,
                    row.query_one(".create-column-type", Input).value,
                )
                for row in self.query(ColumnRow)
            ],
            "from_sql": self.query_one("#create-from-sql", TextArea).text,
            "advanced": advanced,
            **{
                name: self.query_one(f"#create-{name}", Checkbox).value
                for name in ("insert_data", "skip_if_exists", "drop_if_exists")
            },
        }

    def action_cancel(self) -> None:
        for select in self.query(Select):
            if select.expanded:
                select.expanded = False
                select.focus()
                return
        if self.type_matches and self.query_one("#create-type-options").display:
            self.type_matches = ()
            self._show_types()
            return
        self.dismiss(None)

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "create-table-cancel":
            self.action_cancel()
        elif event.button.id == "create-add-column":
            row = ColumnRow()
            await self.query_one("#create-table-schema").mount(row)
            row.query_one(".create-column-name", Input).focus()
        elif event.button.has_class("remove-column"):
            parent = event.button.parent
            if isinstance(parent, ColumnRow):
                await parent.remove()
        elif event.button.id == "create-table-submit":
            draft = self.values()
            try:
                options = creation_options(draft, self.backend)
            except (ValueError, TypeError) as exc:
                self.query_one("#create-table-notice", Static).update(str(exc))
            else:
                self.draft.clear()
                self.draft.update(draft)
                self.dismiss(options)
