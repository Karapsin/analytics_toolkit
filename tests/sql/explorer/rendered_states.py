from __future__ import annotations

import asyncio

import pytest
from analytics_toolkit.sql_explorer import editor as editor_module
from analytics_toolkit.sql_explorer.app import SqlExplorerApp
from analytics_toolkit.sql_explorer.create_table_screen import CreateTableScreen
from analytics_toolkit.sql_explorer.help_text import (
    MOVEMENT_HELP,
    SHORTCUTS_HELP,
    reference_for_width,
)
from textual.widgets import Button, Checkbox, Input, OptionList, Select, Static, TextArea

from tests.sql._support.explorer import FakeSession


def test_search_matches_survive_final_current_line_render_and_scroll() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test(size=(100, 35)) as pilot:
            editor = app.active_workspace.editor
            editor.text = "\t界 alpha alpha\nALPHA " + "x" * 150 + " alpha"
            editor.move_cursor((0, 0))
            await pilot.press("ctrl+f")
            app.query_one("#find-pattern", Input).value = "alpha"
            await pilot.pause()
            assert editor.search_match_count == 4

            def highlighted(location: tuple[int, int]) -> bool:
                offset = editor.wrapped_document.location_to_offset(location)
                x = offset.x - int(editor.scroll_x) + editor.gutter_width
                strip = editor.render_line(offset.y - int(editor.scroll_y)).crop(x, x + 1)
                return any(
                    segment.style
                    and segment.style.bgcolor
                    and segment.style.bgcolor.name == "bright_yellow"
                    for segment in strip
                )

            assert highlighted((0, 3))
            assert highlighted((0, 9))
            assert highlighted((1, 0))
            editor.move_cursor((1, 0))
            await pilot.pause()
            assert highlighted((0, 3))
            assert highlighted((1, 1))
            editor.move_cursor((1, len(editor.document[1])))
            await pilot.pause()
            assert editor.scroll_x > 0
            assert highlighted((1, len(editor.document[1]) - 5))
            editor.select_next_search_match()
            await pilot.pause()
            assert editor.selected_text.casefold() == "alpha"
            assert not highlighted(sorted(editor.selection)[0])
            assert editor.replace_current_search_match("beta")
            await pilot.pause()
            assert editor.search_match_count == 3

    asyncio.run(exercise())


def test_horizontal_thumb_tracks_first_layout_document_changes_and_resize() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test(size=(100, 35)) as pilot:
            editor = app.active_workspace.editor
            for text in ("select " + "x" * 300, "\t界" + "y" * 350):
                editor.text = text
                editor.move_cursor((0, 251))
                await pilot.pause()
                assert editor.horizontal_scrollbar.position == round(editor.scroll_x) > 0
                assert editor.horizontal_scrollbar.window_virtual_size == editor.virtual_size.width
                await pilot.resize_terminal(90, 34)
                await pilot.pause()
                assert editor.horizontal_scrollbar.position == round(editor.scroll_x)
                editor.move_cursor((0, 0))
                await pilot.pause()
                assert editor.horizontal_scrollbar.position == editor.scroll_x == 0
                assert editor.text == text

    asyncio.run(exercise())


def test_creation_flags_and_type_menu_follow_active_field() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test(size=(110, 45)) as pilot:
            screen = CreateTableScreen("gp", "gp", {"rows": [("id", ""), ("name", "")]})
            await app.push_screen(screen)
            await pilot.pause()
            flag = screen.query_one("#create-skip_if_exists", Checkbox)
            assert flag._button.plain == "[ ]"
            flag.focus()
            await pilot.press("space")
            assert flag.value is True
            assert flag._button.plain == "[x]"
            assert screen.values()["skip_if_exists"] is True
            assert flag.label.plain == "skip_if_exists: True"
            other = screen.query_one("#create-drop_if_exists", Checkbox)
            other.value = True
            await pilot.pause()
            assert flag.value is False
            assert flag._button.plain == "[ ]"
            assert screen.query_one("#create-insert_data", Checkbox).disabled
            fields = list(screen.query(".create-column-type"))
            menu = screen.query_one("#create-type-options", OptionList)
            for field in fields:
                field.focus()
                field.value = "B"
                await pilot.pause()
                assert menu.display
                assert menu.region.x == field.region.x
                assert menu.region.width == field.region.width
                assert menu.region.y == field.region.bottom
                await pilot.press("enter")
                assert field.value == "BIGINT"
                assert not menu.display
            fields[0].focus()
            fields[0].value = "B"
            await pilot.pause()
            await pilot.resize_terminal(90, 32)
            await pilot.pause()
            assert menu.region.x == fields[0].region.x
            assert menu.region.width == fields[0].region.width
            await pilot.press("escape")
            assert not menu.display
            screen.query_one("#create-table-source", Select).value = "from_sql"
            await pilot.pause()
            insert = screen.query_one("#create-insert_data", Checkbox)
            assert not insert.disabled
            insert.focus()
            await pilot.press("space")
            assert insert.value is True
            assert insert._button.plain == "[x]"

    asyncio.run(exercise())


def test_creation_validity_tracks_schema_rows_and_rechecks_submission() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        draft = {"table_name": "sandbox.report", "rows": [("id", "INTEGER")]}
        async with app.run_test(size=(110, 45)) as pilot:
            screen = CreateTableScreen("gp", "gp", draft)
            await app.push_screen(screen)
            await pilot.pause()
            submit = screen.query_one("#create-table-submit", Button)
            notice = screen.query_one("#create-table-notice", Static)
            assert not submit.disabled
            data_type = screen.query_one(".create-column-type", Input)
            data_type.value = ""
            await pilot.pause()
            assert submit.disabled
            assert str(notice.renderable) == "Each column needs a name and SQL type."
            # A queued/programmatic submission still revalidates the current form.
            submit.disabled = False
            await screen.on_button_pressed(Button.Pressed(submit))
            assert app.screen is screen
            assert submit.disabled
            assert draft == {"table_name": "sandbox.report", "rows": [("id", "INTEGER")]}
            data_type.value = "BIGINT"
            await pilot.pause()
            assert not submit.disabled
            assert str(notice.renderable) == ""
            screen.query_one("#create-add-column", Button).press()
            await pilot.pause()
            assert submit.disabled
            list(screen.query(".create-column-name"))[-1].value = "label"
            list(screen.query(".create-column-type"))[-1].value = "TEXT"
            await pilot.pause()
            assert not submit.disabled
            list(screen.query(".remove-column"))[-1].press()
            await pilot.pause()
            assert not submit.disabled
            screen.query_one(".remove-column", Button).press()
            await pilot.pause()
            assert submit.disabled
            assert str(notice.renderable) == "Add at least one column."

    asyncio.run(exercise())


def test_creation_validity_tracks_name_source_sql_and_advanced_options() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test(size=(110, 45)) as pilot:
            screen = CreateTableScreen(
                "gp", "gp", {"table_name": "report", "rows": [("id", "INTEGER")]}
            )
            await app.push_screen(screen)
            await pilot.pause()
            submit = screen.query_one("#create-table-submit", Button)
            notice = screen.query_one("#create-table-notice", Static)
            name = screen.query_one("#create-table-name", Input)
            name.value = ""
            await pilot.pause()
            assert submit.disabled
            assert str(notice.renderable) == "Enter a table name."
            name.value = "report"
            screen.query_one("#create-table-source", Select).value = "from_sql"
            await pilot.pause()
            assert submit.disabled
            assert str(notice.renderable) == "Enter source SQL."
            source_sql = screen.query_one("#create-from-sql", TextArea)
            source_sql.text = "SELECT 1 AS id"
            await pilot.pause()
            assert not submit.disabled
            partitions = next(
                field for field in screen.query(Input) if field.name == "gp_partitions"
            )
            partitions.value = "[]"
            await pilot.pause()
            assert submit.disabled
            assert str(notice.renderable) == "gp_partitions must be a JSON object."
            partitions.value = "{"
            await pilot.pause()
            assert submit.disabled
            partitions.value = "{}"
            await pilot.pause()
            assert not submit.disabled
            assert str(notice.renderable) == ""
            source_sql.text = ""
            await pilot.pause()
            assert submit.disabled
            screen.query_one("#create-table-source", Select).value = "table_schema"
            await pilot.pause()
            assert not submit.disabled

    asyncio.run(exercise())


def test_creation_validation_callback_after_cancel_preserves_replacement_screen() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test(size=(110, 45)) as pilot:
            draft = {"table_name": "report", "rows": [("id", "INTEGER")]}
            closed = CreateTableScreen("gp", "gp", draft)
            await app.push_screen(closed)
            await pilot.pause()
            field = closed.query_one(".create-column-type", Input)
            # Queue the actual handler's after-refresh work, then close before
            # it runs. Textual may forward that callback to the next screen.
            field.value = "B"
            closed.on_input_changed(Input.Changed(field, "B"))
            closed.action_cancel()
            closed.action_cancel()
            replacement = CreateTableScreen("gp", "gp", draft)
            await app.push_screen(replacement)
            await pilot.pause()
            assert closed not in app.screen_stack
            closed._refresh_validation()
            closed._position_types()
            assert app.screen is replacement
            assert not replacement.query_one("#create-table-submit", Button).disabled
            assert str(replacement.query_one("#create-table-notice", Static).renderable) == ""
            assert draft == {"table_name": "report", "rows": [("id", "INTEGER")]}

    asyncio.run(exercise())


def test_help_uses_rendered_viewport_and_reflows_complete_text() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test(size=(208, 47)) as pilot:
            app._command_help(["shortcuts"])
            await pilot.pause()
            message = app.active_workspace.result_message
            lines = [line.text for line in message.lines]
            assert any("Escape" in line and "editor/command" in line for line in lines)
            assert any("F8" in line and "disabled)" in line for line in lines)
            original = "".join(message._message.plain.split())
            await pilot.resize_terminal(60, 40)
            await pilot.pause()
            assert "".join("".join(line.text for line in message.lines).split()) == original
            assert all(
                line.cell_length <= message.scrollable_content_region.width
                for line in message.lines
            )
            continuation = next(
                line.text for line in message.lines if "editor/command" in line.text
            )
            assert continuation.startswith(" " * 28)

    asyncio.run(exercise())


@pytest.mark.parametrize("reference", [SHORTCUTS_HELP, MOVEMENT_HELP])
@pytest.mark.parametrize("width", [0, 1, 20, 40, 58])
def test_help_reference_wrapping_retains_every_word_and_fits(reference: str, width: int) -> None:
    rendered = reference_for_width(reference, width)
    assert rendered is not None
    assert "".join(rendered.split()) == "".join(reference.split())
    assert all(len(line) <= max(1, width) for line in rendered.splitlines())
    assert reference_for_width("ordinary error message", width) is None


def test_long_search_overlay_converts_ranges_only_on_the_rendered_row(monkeypatch) -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test(size=(100, 35)) as pilot:
            editor = app.active_workspace.editor
            pattern = "x" * 20_000
            editor.text = pattern + "\n" + "\n".join(["short"] * 100) + "\n" + pattern
            editor.set_search_pattern(pattern)
            await pilot.pause()
            document = editor.wrapped_document
            original = type(document).location_to_offset
            calls = []

            def counted(self, location):
                calls.append(location)
                return original(self, location)

            monkeypatch.setattr(type(document), "location_to_offset", counted)
            for row in range(10):
                editor.render_line(row)
            assert len(calls) <= 4
            assert all(row == 0 for row, _column in calls)
            # Search caches can still describe the prior document until the
            # queued editor-change notification is handled.
            editor.text = "short"
            editor.render_line(0)
            await pilot.pause()

    asyncio.run(exercise())


def test_wrapped_search_uses_original_line_tab_widths_and_zero_width_policy() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test(size=(100, 35)) as pilot:
            editor = app.active_workspace.editor
            editor.show_line_numbers = False
            editor.styles.width = 7
            editor.styles.padding = 0
            editor.soft_wrap = True
            editor.text = "123456789\tX"
            editor.move_cursor((0, 0))
            app.active_workspace.command_input.focus()
            editor.set_search_pattern("789\t")
            await pilot.pause()
            assert editor.wrap_width == 6
            line = editor.render_line(1).crop(0, 6)
            assert line.text == "789   "
            assert all(
                segment.style
                and segment.style.bgcolor
                and segment.style.bgcolor.name == "bright_yellow"
                for segment in line
            )
            editor.text = "a\u0301b"
            editor.set_search_pattern("\u0301")
            await pilot.pause()
            # A zero-cell combining mark does not paint a neighboring glyph.
            line = editor.render_line(0)
            assert not any(
                segment.style
                and segment.style.bgcolor
                and segment.style.bgcolor.name == "bright_yellow"
                for segment in line.crop(1, 2)
            )

    asyncio.run(exercise())


def test_many_short_search_matches_scan_only_the_visible_prefix(monkeypatch) -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test(size=(100, 35)) as pilot:
            editor = app.active_workspace.editor
            editor.text = "x " * 20_000
            editor.set_search_pattern("x")
            await pilot.pause()
            calls = []
            original = editor_module.cell_len

            def counted(character):
                calls.append(character)
                return original(character)

            monkeypatch.setattr(editor_module, "cell_len", counted)
            for row in range(10):
                editor.render_line(row)
            assert len(calls) <= editor.scrollable_content_region.width
            calls.clear()
            editor.scroll_to(x=25_000, animate=False)
            await pilot.pause()
            calls.clear()
            editor.render_line(0)
            assert len(calls) <= round(editor.scroll_x) + editor.scrollable_content_region.width

    asyncio.run(exercise())
