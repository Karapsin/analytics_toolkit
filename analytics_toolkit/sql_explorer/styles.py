from __future__ import annotations

from textual.design import ColorSystem


def explorer_design() -> dict[str, ColorSystem]:
    """Return the fixed dark palette shared by every Explorer entry screen."""

    def dark_system() -> ColorSystem:
        return ColorSystem(
            primary="#1677B8",
            secondary="#303840",
            warning="#D88900",
            error="#E5484D",
            success="#46A758",
            accent="#D88900",
            background="#0E1113",
            surface="#171A1D",
            panel="#20252A",
            boost="#FFFFFF0A",
            dark=True,
            luminosity_spread=0.08,
            text_alpha=0.92,
        )

    return {"dark": dark_system(), "light": dark_system()}


def explorer_css_variables() -> dict[str, str]:
    """Return Explorer colors early enough for Textual's first CSS compilation."""
    return explorer_design()["dark"].generate()


APP_CSS = """
Screen {
    layout: vertical;
}
#tab-strip {
    height: 2;
    overflow-x: auto;
    overflow-y: hidden;
    background: $panel;
    border-bottom: solid $accent;
    scrollbar-size-horizontal: 1;
}
.workspace-tab {
    width: auto;
    height: 1;
}
.tab-select {
    width: auto;
    min-width: 0;
    height: 1;
    border: none;
    padding: 0 1;
    background: $panel;
    color: $text-muted;
    content-align: left middle;
}
.tab-close, .new-tab {
    width: 3;
    min-width: 3;
    height: 1;
    border: none;
    padding: 0;
    background: $panel;
}
.workspace-tab.active .tab-select,
.workspace-tab.active .tab-close {
    background: $accent;
    color: $background;
    text-style: bold;
}
.tab-select:hover, .tab-select:focus,
.tab-close:hover, .tab-close:focus,
.new-tab:hover, .new-tab:focus {
    background: $panel-lighten-2;
    color: $accent-lighten-1;
}
#workspace-stack, .sql-workspace {
    height: 1fr;
}
.query-pane {
    height: 1fr;
    border: solid $panel-lighten-2;
    layers: base overlay;
}
.query-pane:focus-within {
    border: solid $accent;
    background: $panel-lighten-1;
}
#query-editor {
    height: 1fr;
    border: none;
    layer: base;
    scrollbar-size-vertical: 1;
    scrollbar-size-horizontal: 1;
    scrollbar-background: $panel;
    scrollbar-color: $accent-darken-1;
    scrollbar-corner-color: $panel;
}
#editor-status {
    height: 2;
    layer: base;
    border-top: solid $panel-lighten-2;
    padding: 0 1;
    background: $panel;
    color: $text-muted;
    content-align: right middle;
}
.query-pane:focus-within #editor-status {
    border-top: solid $accent;
}
#find-replace-bar {
    display: none;
    dock: right;
    layer: overlay;
    width: 55%;
    min-width: 32;
    max-width: 80;
    height: auto;
    background: $panel;
    border: solid $warning;
    padding: 1;
    margin: 1 1 0 0;
}
#find-pattern, #replace-pattern {
    width: 100%;
    height: 3;
    margin-bottom: 1;
}
#find-pattern:focus, #replace-pattern:focus {
    border: solid $accent;
}
#find-next {
    width: 100%;
    height: 3;
    min-width: 0;
    margin-bottom: 1;
    border: solid $panel-lighten-2;
    background: $surface;
}
#replace-actions {
    height: 3;
}
#replace-current, #replace-all {
    width: 1fr;
    height: 3;
    min-width: 0;
    border: solid $panel-lighten-2;
    background: $surface;
}
#completion-menu {
    display: none;
    layer: overlay;
    width: 32;
    max-width: 48;
    height: auto;
    max-height: 12;
    border: solid $accent;
    background: $panel;
}
.result-pane {
    height: 1fr;
    display: none;
    margin-top: 1;
    border: solid $panel-lighten-2;
}
.result-pane:focus-within {
    border: solid $accent;
    background: $panel-lighten-1;
}
#result-table, #result-message {
    height: 1fr;
}
#result-table {
    scrollbar-size-vertical: 1;
    scrollbar-size-horizontal: 1;
    scrollbar-background: $panel;
    scrollbar-color: $accent-darken-1;
    scrollbar-corner-color: $panel;
}
#result-table:focus, #result-message:focus {
    background: $panel-lighten-1;
}
.command-panel {
    height: 7;
    margin-top: 1;
    border: solid $panel-lighten-2;
    background: $panel;
}
.command-panel:focus-within {
    border: solid $accent;
    background: $panel-lighten-1;
}
#query-summary {
    width: 100%;
    height: 3;
    align-horizontal: right;
}
#query-running-indicator {
    display: none;
    width: 3;
    min-width: 3;
    height: 3;
    background: $panel;
    color: $text-muted;
    content-align: center middle;
}
.query-card {
    display: none;
    width: auto;
    height: 3;
    padding: 0 1;
    border: solid $panel-lighten-2;
    content-align: center middle;
}
#query-outcome.running {
    color: $accent;
    border: solid $accent-darken-1;
}
#query-outcome.success {
    color: $success;
    border: solid $success-darken-1;
}
#query-outcome.error {
    color: $error;
    border: solid $error-darken-1;
}
#query-outcome.cancelled, #query-warning {
    color: $warning;
    border: solid $warning-darken-1;
}
#query-warning {
    text-style: bold;
}
.interrupt {
    width: 14;
    min-width: 14;
    height: 3;
    margin: 0;
    padding: 0 1;
    background: $panel;
    color: $error;
    border: solid $error;
    text-style: bold;
}
.interrupt:hover, .interrupt:focus {
    background: $error 20%;
}
.interrupt:disabled {
    color: $text-disabled;
    border: solid $error-darken-3;
    background: $panel;
    text-style: none;
}
#notice {
    height: 1;
    padding: 0 1;
}
#command-row {
    height: 1;
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
EditableInput > .input--selection {
    background: $accent 45%;
    color: $text;
    text-style: bold;
}

#navigation-select-directory.armed {
    background: $panel-lighten-2;
    color: $accent-lighten-1;
    text-style: bold;
}
"""

__all__ = ["APP_CSS", "explorer_css_variables", "explorer_design"]
