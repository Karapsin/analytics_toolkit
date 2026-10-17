from __future__ import annotations

from types import SimpleNamespace

import pytest
from agent_tools.sql_explorer_visual_scene import _completion_readiness
from analytics_toolkit.sql_explorer.completion_commands import SqlExplorerCompletionCommandsMixin
from analytics_toolkit.sql_explorer.editor import SqlEditor
from rich.segment import Segment
from textual.geometry import Region
from textual.strip import Strip


class MenuState(SimpleNamespace):
    """Match CompletionMenu's display-derived open state."""

    @property
    def is_open(self) -> bool:
        return self.styles.display != "none"


def _workspace() -> SimpleNamespace:
    return SimpleNamespace(
        editor=SimpleNamespace(
            is_mounted=True,
            cursor_count=1,
            selection=SimpleNamespace(is_empty=True),
            cursor_location=(20, 0),
            scroll_y=0,
            scroll_x=0,
            gutter_width=3,
            scrollable_content_region=Region(2, 3, 60, 12),
            wrapped_document=SimpleNamespace(location_to_offset=lambda point: (point[1], point[0])),
        ),
        completion_menu=MenuState(
            is_mounted=True,
            display=True,
            visible=True,
            region=Region(5, 4, 32, 5),
            suggestions=("customer_id", "customer_name", "created_at"),
            styles=SimpleNamespace(display="block"),
            parent=SimpleNamespace(content_region=Region(2, 3, 60, 12)),
        ),
    )


@pytest.mark.parametrize("cause", ["offscreen", "insufficient_height"])
def test_completion_position_recovers_after_layout_becomes_valid(cause: str) -> None:
    workspace = _workspace()
    app = SimpleNamespace(screen=SimpleNamespace(content_region=Region(0, 0, 80, 20)))
    position = SqlExplorerCompletionCommandsMixin._position_completion
    if cause == "insufficient_height":
        workspace.editor.cursor_location = (0, 0)
        workspace.editor.scrollable_content_region = Region(2, 3, 60, 2)
    position(app, workspace)
    assert workspace.completion_menu.styles.display == "none"
    assert not workspace.completion_menu.is_open
    workspace.editor.cursor_location = (0, 0)
    workspace.editor.scrollable_content_region = Region(2, 3, 60, 12)
    position(app, workspace)
    styles = workspace.completion_menu.styles
    assert styles.display == "block"
    assert styles.width == 32
    assert styles.height == 5
    assert styles.offset == (3, 1)


@pytest.mark.parametrize(
    "failure", ["none", "closed", "hidden", "unmounted", "clipped", "uncomposed"]
)
def test_completion_readiness_requires_current_visible_composed_popup(failure: str) -> None:
    workspace = _workspace()
    workspace.editor.cursor_location = (0, 0)
    menu = workspace.completion_menu
    if failure == "closed":
        menu.styles.display = "none"
        menu.suggestions = ()
    elif failure == "hidden":
        menu.display = False
        menu.visible = False
    elif failure == "unmounted":
        menu.is_mounted = False
    elif failure == "clipped":
        menu.region = Region(5, 14, 32, 5)
    rows = [Strip([Segment(" " * 80)]) for _ in range(20)]
    if failure != "uncomposed":
        for row, suggestion in enumerate(menu.suggestions, start=5):
            rows[row] = Strip([Segment(" " * 5 + suggestion + " " * 40)])
    app = SimpleNamespace(
        active_workspace=workspace,
        screen=SimpleNamespace(
            content_region=Region(0, 0, 80, 20),
            _compositor=SimpleNamespace(render_strips=lambda: rows),
        ),
    )
    details = {"conditions": {}}
    _completion_readiness(app, details)
    assert all(details["conditions"].values()) == (failure == "none")


@pytest.mark.parametrize("blocked", ["closed", "multicursor", "selection"])
def test_position_does_not_reopen_dismissed_or_ineligible_completion(blocked: str) -> None:
    workspace = _workspace()
    menu = workspace.completion_menu
    menu.styles.display = "none"
    workspace.editor.cursor_location = (0, 0)
    if blocked == "closed":
        menu.suggestions = ()
    elif blocked == "multicursor":
        workspace.editor.cursor_count = 2
    else:
        workspace.editor.selection.is_empty = False
    app = SimpleNamespace(screen=SimpleNamespace(content_region=Region(0, 0, 80, 20)))
    SqlExplorerCompletionCommandsMixin._position_completion(app, workspace)
    assert not menu.is_open


def test_idle_repositions_retained_suggestions_even_while_popup_is_hidden() -> None:
    workspace = _workspace()
    workspace.completion_menu.styles.display = "none"
    workspace.editor.cursor_location = (0, 0)
    app = SimpleNamespace(
        screen=SimpleNamespace(content_region=Region(0, 0, 80, 20)),
        active_workspace=workspace,
    )
    app._position_completion = lambda target: (
        SqlExplorerCompletionCommandsMixin._position_completion(app, target)
    )
    editor = workspace.editor
    editor.app = app
    editor._sync_horizontal_scrollbar = lambda: None
    editor.history = SimpleNamespace(undo_stack=[], redo_stack=[])
    editor._cursor_edits = []
    editor._prune_cursor_history = lambda: SqlEditor._prune_cursor_history(editor)
    SqlEditor.on_idle(editor)
    assert workspace.completion_menu.is_open
    workspace.completion_menu.suggestions = ()
    workspace.completion_menu.styles.display = "none"
    SqlEditor.on_idle(editor)
    assert not workspace.completion_menu.is_open
