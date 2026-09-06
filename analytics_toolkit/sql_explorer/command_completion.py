"""Local command completion without database work or command execution."""

from __future__ import annotations

COMMAND_NAMES = (
    "cancel",
    "clear",
    "confirm",
    "cp",
    "create_table",
    "db",
    "exit",
    "exit!",
    "format",
    "help",
    "mode",
    "mv",
    "mvs",
    "open",
    "pst",
    "q",
    "q!",
    "quit",
    "run",
    "save",
    "shortcut",
    "to_csv",
    "to_excel",
    "wq",
)
COMMAND_ARGUMENTS = {
    "mode": ("exploratory", "navigation"),
    "confirm": ("on", "off", "toggle"),
}


def command_suggestions(
    value: str, cursor: int, database_keys: tuple[str, ...] = ()
) -> tuple[int, tuple[str, ...]]:
    """Complete the current unquoted word, preserving optional colon and suffix."""
    if cursor < len(value) and not value[cursor].isspace():
        return cursor, ()
    before = value[:cursor]
    start = before.rfind(" ") + 1
    command_start = len(value) - len(value.lstrip())
    choices: tuple[str, ...]
    if start <= command_start:
        start = command_start + int(value[command_start : command_start + 1] == ":")
        choices = COMMAND_NAMES
    else:
        parts = value[:start].strip().lstrip(":").split()
        choices = (
            database_keys
            if parts == ["db"]
            else COMMAND_ARGUMENTS.get(parts[0], ())
            if len(parts) == 1
            else ()
        )
    prefix = value[start:cursor].casefold()
    return start, tuple(choice for choice in choices if choice.startswith(prefix))
