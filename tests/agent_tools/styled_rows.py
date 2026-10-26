from __future__ import annotations

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from agent_tools.sql_explorer_visual_scene import VisualSession
from analytics_toolkit.sql_explorer.app import SqlExplorerApp
from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from textual.app import App
from textual.geometry import Region
from textual.strip import Strip
from textual.widgets import Static

from agent_tools import sql_explorer_render_diagnostics as diagnostics

if TYPE_CHECKING:
    from textual.app import ComposeResult


class StyledRowsApp(App[None]):
    CSS = """
    #probe {
        offset: 3 2;
        width: 19;
        height: 7;
        border: solid #456789;
        padding: 1 2;
        color: #abcdef;
        background: #123456;
    }
    """

    def compose(self) -> ComposeResult:
        text = Text("界e\u0301   1")
        text.stylize("bold underline #fedcba", 1, 3)
        yield Static(text, id="probe")


def test_native_styled_content_row_maps_border_padding_and_nonzero_origin_once() -> None:
    async def exercise() -> None:
        app = StyledRowsApp()
        async with app.run_test(size=(40, 15)) as pilot:
            await pilot.pause()
            widget = app.query_one("#probe", Static)
            content, outer = widget.content_region, widget.region
            raw = widget.render_line(0)
            original_segments = list(raw)
            before = (app.focused, widget.region, widget.content_region, widget.scroll_offset)
            composed = app.screen._compositor.render_strips()
            snapshot = diagnostics.styled_content_row(widget)
            expected = diagnostics.strip_record(
                composed[content.y].crop(content.x, content.right),
                x=content.x,
                y=content.y,
                width=content.width,
            )
            assert snapshot["complete"]
            assert snapshot["omissions"] == []
            assert snapshot["widget_local_clip"] == {
                "x": content.x - outer.x,
                "y": content.y - outer.y,
                "width": content.width,
                "height": 1,
            }
            assert content.x > outer.x > 0
            assert content.y > outer.y > 0
            assert snapshot["row"]["cells"] == content.width
            assert snapshot["row"]["text"].startswith("界e\u0301   1")
            assert snapshot["row"]["text"].endswith(" ")
            assert diagnostics.rendered_rows_equal(snapshot["row"], expected)
            _assert_clipped_styled_rows(snapshot["row"], composed[content.y], content)
            assert list(raw) == original_segments
            assert list(widget.render_line(0)) == original_segments
            assert before == (
                app.focused,
                widget.region,
                widget.content_region,
                widget.scroll_offset,
            )

    asyncio.run(exercise())


def _assert_clipped_styled_rows(row: dict[str, Any], composed: Strip, content: Region) -> None:
    for left, width in ((1, 4), (2, 3), (6, 3)):
        x = content.x + left
        clipped = diagnostics._caret_cells([row], x=x, y=content.y, width=width)
        expected = diagnostics.strip_record(
            composed.crop(x, x + width), x=x, y=content.y, width=width
        )
        assert clipped is not None
        assert clipped["cells"] == width
        assert diagnostics.rendered_rows_equal(clipped, expected)


def test_equal_styles_ignore_only_segment_boundaries_and_retain_combining_marks() -> None:
    style = Style(color="#123456", bgcolor="#abcdef", underline=True)
    whole = Strip([Segment("界e\u0301  ", style)])
    split = Strip(
        [Segment("界", style), Segment("e", style), Segment("\u0301", style), Segment("  ", style)]
    )
    left = diagnostics.strip_record(whole, x=5, y=7, width=8)
    right = diagnostics.strip_record(split, x=5, y=7, width=8)
    before = deepcopy((left, right))
    assert diagnostics.rendered_rows_equal(left, right)
    assert (left, right) == before


@pytest.mark.parametrize(
    "damage",
    ["position", "cells", "text", "color", "flag", "span_position", "truncated", "missing"],
)
def test_styled_row_comparison_rejects_mapping_trailing_cell_and_style_loss(damage: str) -> None:
    row = diagnostics.strip_record(
        Strip([Segment("1   ", Style(color="#123456", bgcolor="#abcdef"))]),
        x=4,
        y=8,
        width=4,
    )
    changed = deepcopy(row)
    if damage == "position":
        changed["x"] += 1
    elif damage == "cells":
        changed["cells"] -= 1
    elif damage == "text":
        changed["text"] = changed["text"][:-1]
    elif damage == "color":
        changed["spans"][0]["background"] = "#654321"
    elif damage == "flag":
        changed["spans"][0]["flags"]["bold"] = True
    elif damage == "span_position":
        changed["spans"][0]["x"] += 1
    elif damage == "truncated":
        changed["truncated"] = True
    else:
        changed = None
    assert not diagnostics.rendered_rows_equal(row, changed)
    assert not diagnostics.rendered_rows_equal(changed, row)


def test_styled_collection_failure_and_cell_limit_are_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unavailable = diagnostics.styled_content_row(object())
    assert not unavailable["complete"]
    assert unavailable["row"] is None
    assert unavailable["missing"] == ["styled_widget:AttributeError"]
    strip = Strip([Segment("1   ", Style(bgcolor="#123456"))])
    clips = []

    def render_lines(clip: Region) -> list[Strip]:
        clips.append(clip)
        return [strip.crop(clip.x, clip.right)]

    widget = SimpleNamespace(
        content_region=Region(7, 9, 4, 1),
        region=Region(7, 9, 4, 1),
        render_lines=render_lines,
    )
    monkeypatch.setattr(diagnostics, "MAX_CELLS", 3)
    limited = diagnostics.styled_content_row(widget)
    assert not limited["complete"]
    assert limited["omissions"] == ["styled_widget:cell_limit"]
    assert limited["row"]["text"] == "1  "
    assert clips == [Region(0, 0, 3, 1)]
    monkeypatch.setattr(diagnostics, "MAX_CELLS", 208)
    widget.render_lines = lambda _clip: [Strip([Segment("1")])]
    short = diagnostics.styled_content_row(widget)
    assert not short["complete"]
    assert short["omissions"] == ["styled_widget:row_width"]
    assert short["row"]["text"] == "1"


def test_production_tabs_restore_each_workspaces_remembered_focus() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(VisualSession())
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            first = app.active_workspace
            first.editor.focus()
            await pilot.pause()
            second = app._add_workspace()
            await pilot.pause()
            assert app.active_workspace is second
            assert app.focused is second.editor
            second.command_input.focus()
            await pilot.pause()
            app._activate_tab(first.tab_id)
            await pilot.pause()
            assert app.focused is first.editor
            assert second.last_focused is second.command_input
            app._activate_tab(second.tab_id)
            await pilot.pause()
            assert app.focused is second.command_input
            assert first.last_focused is first.editor

    asyncio.run(exercise())
