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
    layers: base overlay;
}
#query-pane:focus-within {
    border: double $accent;
    background: $panel-lighten-1;
}
#query-editor {
    height: 1fr;
    border: none;
    layer: base;
}
#find-replace-bar {
    display: none;
    dock: right;
    layer: overlay;
    width: 12.5%;
    min-width: 24;
    max-width: 48;
    height: 14;
    background: $panel;
    border: round $accent;
    padding: 0 1;
}
#find-pattern, #replace-pattern {
    width: 100%;
    height: 3;
}
#find-next {
    width: 100%;
    height: 3;
    min-width: 0;
}
#replace-actions {
    height: 3;
}
#replace-current, #replace-all {
    width: 1fr;
    height: 3;
    min-width: 0;
}
#completion-menu {
    display: none;
    layer: overlay;
    width: 32;
    max-width: 48;
    height: auto;
    max-height: 12;
    border: round $accent;
    background: $panel;
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
    height: 8;
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
#interrupt {
    width: 16;
    height: 3;
    margin-left: 1;
}
#command-input:focus {
    background: $panel-lighten-1;
    color: $text;
    text-style: bold;
}
"""

__all__ = ["APP_CSS"]
