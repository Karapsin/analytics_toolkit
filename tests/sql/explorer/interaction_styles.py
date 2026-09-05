from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pandas as pd
from analytics_toolkit.sql_explorer.app import SqlExplorerApp
from analytics_toolkit.sql_explorer.picker import DatabasePickerApp
from analytics_toolkit.sql_explorer.widgets import FileNavigationScreen
from textual.color import Color
from textual.document._document import Selection
from textual.widgets import Button, OptionList, Tree

from tests.sql.explorer.app import FakeSession

if TYPE_CHECKING:
    from pathlib import Path


def test_startup_picker_uses_amber_selection_and_subtle_independent_hover() -> None:
    async def exercise() -> None:
        application = DatabasePickerApp((("gp", "Greenplum"), ("lake", "Trino")))
        async with application.run_test() as pilot:
            options = application.query_one(OptionList)
            colors = application.get_css_variables()
            amber = Color.parse(colors["accent"])
            hover = Color.parse(colors["panel-lighten-2"])
            await pilot.press("down")
            assert options.highlighted == 1
            neutral_row = options.render_line(0)
            await pilot.hover("#database-options", offset=(2, 1))
            assert options.render_line(0) != neutral_row
            assert any(
                segment.style and segment.style.bgcolor == hover.rich_color
                for segment in options.render_line(0)
            )
            assert options._mouse_hovering_over == 0
            assert options.highlighted == 1
            assert options.get_component_styles("option-list--option-hover").background == hover
            assert any(
                segment.style and segment.style.bgcolor == amber.rich_color
                for segment in options.render_line(1)
            )
            await pilot.hover(offset=(0, 0))
            assert options._mouse_hovering_over is None
            assert options.render_line(0) == neutral_row

    asyncio.run(exercise())


def test_controls_use_amber_while_text_selection_retains_translucency() -> None:
    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        async with application.run_test(size=(120, 40)) as pilot:
            workspace = application.active_workspace
            colors = application.get_css_variables()
            amber = Color.parse(colors["accent"])
            hover = Color.parse(colors["panel-lighten-2"])
            application.show_dataframe(pd.DataFrame({"value": [1, 2]}))
            table = workspace.result_table
            table.focus()
            await pilot.press("down")
            assert table.cursor_coordinate.row == 1
            for component in (
                "datatable--cursor",
                "datatable--fixed-cursor",
                "datatable--header-cursor",
            ):
                assert table.get_component_styles(component).background == amber
            for component in ("datatable--hover", "datatable--header-hover"):
                assert table.get_component_styles(component).background == hover
            workspace.completion_menu.open(("customer_id", "customer_name"))
            workspace.completion_menu.move_highlight(1)
            assert workspace.completion_menu.highlighted == 1
            assert (
                workspace.completion_menu.get_component_styles(
                    "option-list--option-highlighted"
                ).background
                == amber
            )
            workspace.editor.text = "select 1"
            workspace.editor.selection = Selection((0, 0), (0, 6))
            workspace.editor.focus()
            await pilot.pause()
            assert workspace.editor._theme.selection_style.bgcolor != amber.rich_color
            workspace.command_input.value = "format"
            workspace.command_input.action_select_all()
            assert workspace.command_input.get_component_styles(
                "input--selection"
            ).background == amber.with_alpha(0.45)

    asyncio.run(exercise())


def test_tree_and_tab_buttons_share_hover_and_focus_colors(tmp_path: Path) -> None:
    (tmp_path / "query.sql").write_text("select 1", encoding="utf-8")

    async def exercise() -> None:
        application = SqlExplorerApp(FakeSession())
        async with application.run_test(size=(120, 40)) as pilot:
            colors = application.get_css_variables()
            amber = Color.parse(colors["accent"])
            hover = Color.parse(colors["panel-lighten-2"])
            add = application.query_one("#new-tab", Button)
            await pilot.hover("#new-tab")
            assert add.styles.background == hover
            add.focus()
            await pilot.pause()
            assert add.styles.background == amber
            picker = FileNavigationScreen(tmp_path)
            application.push_screen(picker)
            await pilot.pause()
            tree = picker.query_one(Tree)
            tree.focus()
            await pilot.press("down")
            assert tree.get_component_styles("tree--cursor").background == amber
            assert tree.get_component_styles("tree--highlight-line").background == hover

    asyncio.run(exercise())
