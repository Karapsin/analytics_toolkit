"""Export one selected journal action through the existing save dialogs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .file_commands import NewFileScreen
from .widgets import FileNavigationScreen

if TYPE_CHECKING:
    from textual.screen import Screen


def export_text(record: dict[str, Any], suffix: str) -> str:
    if suffix == ".sql":
        text = record.get("user_sql")
        if not isinstance(text, str):
            message = "This action has no user-visible SQL to export."
            raise ValueError(message)
        return text
    return json.dumps(record, ensure_ascii=False, indent=2) + "\n"


def export_record(screen: Screen[Any], record: dict[str, Any], suffix: str) -> None:
    content = export_text(record, suffix)

    def selected_directory(filename: str, directory: Path | None) -> None:
        if directory is None:
            return
        try:
            with (directory / filename).open("x", encoding="utf-8", newline="") as stream:
                stream.write(content)
        except OSError as exc:
            screen.notify(f"Journal export failed: {exc}", severity="error")
        else:
            screen.notify(f"Exported {directory / filename}")

    def selected_name(filename: str | None) -> None:
        if filename is not None:
            screen.app.push_screen(
                FileNavigationScreen(Path.cwd(), select_directory=True),
                lambda directory: selected_directory(filename, directory),
            )

    screen.app.push_screen(
        NewFileScreen(
            suffix=suffix,
            title=f"Export journal {suffix[1:].upper()}",
            placeholder=f"query{suffix}",
        ),
        selected_name,
    )
