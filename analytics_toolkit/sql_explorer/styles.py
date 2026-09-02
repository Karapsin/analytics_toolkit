from __future__ import annotations

APP_CSS = """
Screen {
    layout: vertical;
}
#workspace {
    height: 1fr;
}
#query-pane {
    height: 1fr;
    border: round $panel-lighten-2;
}
#query-pane:focus-within {
    border: double $accent;
    background: $panel-lighten-1;
}
#query-editor {
    height: 1fr;
    border: none;
}
#find-replace-bar {
    display: none;
    height: 6;
    background: $panel;
    border-bottom: solid $accent;
}
#find-row, #replace-row {
    height: 3;
}
#find-pattern, #replace-pattern {
    width: 1fr;
}
#find-next, #replace-current {
    width: 14;
}
#replace-all {
    width: 16;
}
#result-pane {
    height: 1fr;
    display: none;
    border: round $panel-lighten-2;
}
#result-pane:focus-within {
    border: double $accent;
    background: $panel-lighten-1;
}
#result-table, #result-message {
    height: 1fr;
}
#result-table:focus, #result-message:focus {
    background: $panel-lighten-1;
}
#command-panel {
    height: 5;
    border: round $panel-lighten-2;
    background: $panel;
}
#command-panel:focus-within {
    border: double $accent;
    background: $panel-lighten-1;
}
#session-status, #notice {
    height: 1;
    padding: 0 1;
}
#command-input {
    height: 1;
    border: none;
    padding: 0 1;
}
#command-input:focus {
    background: $panel-lighten-1;
    color: $text;
    text-style: bold;
}
"""

__all__ = ["APP_CSS"]
