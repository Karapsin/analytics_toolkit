"""Searchable, asynchronous journal browser."""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING, Any, ClassVar, Optional

from rich.text import Text
from textual import work
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, OptionList, Static, TextArea
from textual.widgets.option_list import Option
from textual.worker import get_current_worker

from .inputs import EditableInput
from .journal_exports import export_record
from .journal_reader import JournalCursor, JournalPage, read_page, read_record

if TYPE_CHECKING:
    from collections.abc import Callable

    from textual.app import ComposeResult

    from .journal import QueryJournal


class JournalScreen(ModalScreen[Optional[str]]):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "close", "Close", show=False),
        Binding("ctrl+h", "close", "Close", show=False, priority=True),
        Binding("ctrl+f", "search", "Search", show=False, priority=True),
    ]
    CSS = """
    JournalScreen { align: center middle; }
    #journal-dialog {
        width: 92%; height: 90%; border: solid $accent; background: $panel; padding: 0 1;
    }
    #journal-title { height: 2; padding-top: 1; color: $accent; }
    #journal-search-row, #journal-buttons { height: 3; }
    #journal-search { width: 1fr; }
    #journal-body { height: 1fr; }
    #journal-list { width: 42%; height: 1fr; border: solid $primary; }
    #journal-detail-pane { width: 58%; height: 1fr; padding-left: 1; }
    #journal-details { height: 6; overflow-y: auto; }
    #journal-next-sql { width: 100%; height: 3; }
    #journal-sql { height: 1fr; border: solid $primary; }
    #journal-status { height: 2; }
    #journal-help { height: 1; color: $text-muted; }
    """

    def __init__(self, journal: QueryJournal, alias: str) -> None:
        super().__init__()
        self.journal = journal
        self.alias = alias
        self.records: list[dict[str, Any]] = []
        self.include_internal = False
        self.cursor: JournalCursor | None = None
        self._pages: list[JournalCursor | None] = [None]
        self._record: dict[str, Any] | None = None
        self._selection_generation = 0
        self._generation = 0
        self._sql_choices: list[tuple[str, str]] = []
        self._sql_metadata: list[dict[str, Any]] = []
        self._sql_index = 0
        self._user_sql: str | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="journal-dialog"):
            yield Static(f"Query journal · {self.alias}", id="journal-title", markup=False)
            with Horizontal(id="journal-search-row"):
                yield EditableInput(
                    placeholder="Search SQL, filename, catalog, schema or table",
                    id="journal-search",
                )
                yield Button("User actions", id="journal-filter")
            with Horizontal(id="journal-body"):
                yield OptionList(id="journal-list")
                with Vertical(id="journal-detail-pane"):
                    yield Static(
                        "Select an entry to inspect its SQL and metadata.",
                        id="journal-details",
                        markup=False,
                    )
                    yield Button("No SQL selected", id="journal-next-sql", disabled=True)
                    yield TextArea("", read_only=True, show_line_numbers=True, id="journal-sql")
            yield Static("Loading journal…", id="journal-status", markup=False)
            with Horizontal(id="journal-buttons"):
                yield Button("Open in new tab", id="journal-open", disabled=True)
                yield Button("Previous", id="journal-previous", disabled=True)
                yield Button("Next", id="journal-more", disabled=True)
                yield Button("Export SQL", id="journal-export-sql", disabled=True)
                yield Button("Export JSON", id="journal-export-json", disabled=True)
                yield Button("Refresh", id="journal-refresh")
                yield Button("Close", id="journal-close")
            yield Static(
                "↑/↓ select · Enter open · Ctrl+F search · Ctrl+H / Esc close", id="journal-help"
            )

    def on_mount(self) -> None:
        self.reload()
        self.query_one("#journal-list", OptionList).focus()

    def reload(self, *, more: bool = False, previous: bool = False) -> None:
        self._generation += 1
        self._selection_generation += 1
        if more and self.cursor is not None:
            self._pages.append(self.cursor)
        elif previous and len(self._pages) > 1:
            self._pages.pop()
        else:
            self._pages = [None]
        self.records.clear()
        self.cursor = None
        self.query_one("#journal-list", OptionList).clear_options()
        self._show_record(None)
        self.query_one("#journal-more", Button).disabled = True
        self.query_one("#journal-previous", Button).disabled = len(self._pages) <= 1
        self.query_one("#journal-status", Static).update("Loading journal…")
        self._load(
            self._generation,
            self.query_one("#journal-search", Input).value,
            self._pages[-1],
            include_internal=self.include_internal,
        )

    @work(thread=True, group="journal-pages", exclusive=True, exit_on_error=False)
    def _load(
        self, generation: int, query: str, cursor: JournalCursor | None, *, include_internal: bool
    ) -> None:
        page = read_page(
            self.journal,
            self.alias,
            query=query,
            include_internal=include_internal,
            before=cursor,
        )
        if not get_current_worker().is_cancelled:
            self.app.call_from_thread(self._receive_page, generation, page)

    def _receive_page(self, generation: int, page: JournalPage) -> None:
        if generation != self._generation or not self.is_mounted:
            return
        options = self.query_one("#journal-list", OptionList)
        for record in page.records:
            index = len(self.records)
            self.records.append(record)
            actions = record["query_types"]
            label = (
                f"{record.get('started_at', '')} · {record.get('outcome', '')}\n"
                f"{record.get('origin', '')} · {actions} · "
                f"{record.get('source_file') or 'Untitled'}"
            )
            options.add_option(Option(Text(label), id=str(index)))
        self.cursor = page.cursor
        self.query_one("#journal-more", Button).disabled = self.cursor is None
        status = (
            f"Page {len(self._pages)} · {len(self.records)} entries."
            if self.records
            else "No matching journal entries."
        )
        self.query_one("#journal-status", Static).update(f"{status} {page.warning}")
        if self.records and options.highlighted is None:
            options.highlighted = 0
        if self.records and options.highlighted is not None:
            self._select_record(self.records[options.highlighted]["action_id"])

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "journal-search":
            self.reload()

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if event.option_list.id == "journal-list" and event.option_index < len(self.records):
            self._select_record(self.records[event.option_index]["action_id"])

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list.id == "journal-list":
            self._open()

    def _select_record(self, action_id: str) -> None:
        self._selection_generation += 1
        self._show_record(None)
        self._load_record(self._selection_generation, action_id)

    @work(thread=True, group="journal-details", exclusive=True, exit_on_error=False)
    def _load_record(self, generation: int, action_id: str) -> None:
        try:
            record = read_record(self.journal, self.alias, action_id)
        except (OSError, sqlite3.Error, ValueError) as exc:
            record = None
            error = f"Cannot load entry: {exc}"
        else:
            error = ""
        if not get_current_worker().is_cancelled:
            self.app.call_from_thread(self._receive_record, generation, record, error)

    def _receive_record(self, generation: int, record: dict[str, Any] | None, error: str) -> None:
        if generation != self._selection_generation or not self.is_mounted:
            return
        self._show_record(record)
        if error:
            self.query_one("#journal-status", Static).update(error)

    def _show_record(self, record: dict[str, Any] | None) -> None:
        self._record = record
        self._sql_choices = []
        self._sql_metadata = []
        self._sql_index = 0
        self._user_sql = None
        if record is not None:
            if isinstance(record.get("user_sql"), str):
                self._user_sql = record["user_sql"]
                self._sql_choices.append(("User-visible SQL", record["user_sql"]))
                self._sql_metadata.append(record)
            for item in record["submissions"]:
                if isinstance(item, dict) and isinstance(item.get("sql"), str):
                    self._sql_metadata.append(item)
                    self._sql_choices.append(
                        (f"Executed SQL · {item.get('outcome', 'unknown')}", item["sql"])
                    )
        else:
            self.query_one("#journal-details", Static).update(
                "Select an entry to inspect its SQL and metadata."
            )
        self.query_one("#journal-export-sql", Button).disabled = self._user_sql is None
        self.query_one("#journal-export-json", Button).disabled = record is None
        self._show_sql()

    def _show_sql(self) -> None:
        button = self.query_one("#journal-next-sql", Button)
        button.disabled = len(self._sql_choices) <= 1
        label, sql = (
            self._sql_choices[self._sql_index] if self._sql_choices else ("No SQL selected", "")
        )
        button.label = (
            f"{label} ({self._sql_index + 1}/{len(self._sql_choices)}) · Next"
            if self._sql_choices
            else label
        )
        self.query_one("#journal-sql", TextArea).load_text(sql)
        if self._record is not None:
            detail = self._sql_metadata[self._sql_index] if self._sql_metadata else self._record
            self.query_one("#journal-details", Static).update(_detail_text(self._record, detail))
        self.query_one("#journal-open", Button).disabled = not sql

    def _open(self) -> None:
        if self._sql_choices:
            self.dismiss(
                self._user_sql
                if self._user_sql is not None
                else self._sql_choices[self._sql_index][1]
            )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        actions: dict[str, Callable[[], None]] = {
            "journal-close": self.action_close,
            "journal-open": self._open,
            "journal-refresh": self.reload,
            "journal-more": lambda: self.reload(more=True),
            "journal-previous": lambda: self.reload(previous=True),
        }
        if event.button.id in {"journal-export-sql", "journal-export-json"}:
            if self._record is not None:
                suffix = ".sql" if event.button.id == "journal-export-sql" else ".json"
                export_record(self, self._record, suffix)
        elif event.button.id == "journal-filter":
            self.include_internal = not self.include_internal
            event.button.label = "All actions" if self.include_internal else "User actions"
            self.reload()
        elif event.button.id == "journal-next-sql":
            self._sql_index = (self._sql_index + 1) % len(self._sql_choices)
            self._show_sql()
        elif event.button.id in actions:
            actions[str(event.button.id)]()

    def action_search(self) -> None:
        self.query_one("#journal-search", Input).focus()

    def action_close(self) -> None:
        self.dismiss(None)


def _detail_text(record: dict[str, Any], detail: dict[str, Any]) -> str:
    duration = detail.get("elapsed_seconds")
    elapsed = f"{duration:.3f}s" if isinstance(duration, (float, int)) else "unfinished"
    statements = detail.get("statements", [])
    actions = ", ".join(dict.fromkeys(s.get("action", "unknown") for s in statements))
    objects = list(
        dict.fromkeys(
            ".".join(str(obj[key]) for key in ("catalog", "schema", "name") if obj.get(key))
            for statement in statements
            for obj in statement.get("objects", [])
        )
    )
    lines = [
        f"{detail.get('outcome', 'unknown')} · {elapsed} · {record['origin']} · {actions}",
        f"Started: {detail.get('started_at', '')}",
        f"Source: {record.get('source_file') or 'Untitled'}",
        "Objects: " + (", ".join(objects) or "none identified"),
    ]
    if record.get("context"):
        lines.append("Context: " + json.dumps(record["context"], ensure_ascii=False))
    if detail.get("error"):
        lines.append("Error: " + json.dumps(detail["error"], ensure_ascii=False))
    return "\n".join(lines)
