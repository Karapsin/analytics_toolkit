from __future__ import annotations

import asyncio

import pytest
from analytics_toolkit.sql_explorer.app import SqlExplorerApp
from analytics_toolkit.sql_explorer.completion import CompletionRequest, CompletionResult
from analytics_toolkit.sql_explorer.editor_actions import completion_text

from tests.sql.explorer.app import FakeSession
from tests.sql.explorer.completion import _install_stub


@pytest.mark.parametrize(
    ("backend", "qualifier", "prefix", "narrow"),
    [
        ("gp", "", "sa", "n"),
        ("ch", "", "sa", "n"),
        ("trino", "", "ice", "b"),
        ("trino", "iceberg.", "sa", "n"),
    ],
)
@pytest.mark.parametrize("mode", ["single", "filtered", "async_single", "async_filtered"])
def test_namespace_completion_leaves_caret_ready_for_qualification(
    backend: str,
    qualifier: str,
    prefix: str,
    narrow: str,
    mode: str,
) -> None:
    async def exercise() -> None:
        session = FakeSession()
        session.database.backend = backend
        app = SqlExplorerApp(session)
        async with app.run_test() as pilot:
            stub = _install_stub(app)
            stub.table_cache = ()
            stub.catalogs = ("iceberg",)
            workspace = app.active_workspace
            editor = workspace.editor
            catalog = "iceberg" if qualifier else None
            is_catalog = backend == "trino" and not qualifier
            names = ("iceberg", "iceland") if is_catalog else ("sandbox", "sales")
            expected = names[0]
            available = (expected,) if mode.endswith("single") else names
            initial = () if mode.startswith("async") else available
            if is_catalog:
                stub.catalogs = initial
            else:
                stub.schemas[catalog] = initial
            editor.text = "select * from " + qualifier + prefix
            editor.cursor_location = (0, len(editor.text))
            await pilot.press("tab")
            if mode.startswith("async"):
                assert editor.text.endswith(prefix)
                if is_catalog:
                    stub.catalogs = available
                else:
                    stub.schemas[catalog] = available
                app._receive_namespace(
                    CompletionResult(
                        CompletionRequest("gp", backend, "catalog" if is_catalog else "schema", ""),
                        1,
                        available,
                    )
                )
            if mode.endswith("filtered"):
                assert workspace.completion_menu.is_open
                await pilot.press(narrow)
                assert workspace.completion_menu.suggestions == (expected,)
                await pilot.press("tab" if mode == "filtered" else "enter")
            assert editor.text == "select * from " + qualifier + expected + "."
            assert editor.cursor_location == (0, len(editor.text))
            assert editor.text.endswith(expected + ".")
            request = app._completion_at_cursor().request
            assert (request.catalog if is_catalog else request.schema) == expected

    asyncio.run(exercise())


def test_star_shortcut_accepts_missing_or_extra_trailing_whitespace() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test() as pilot:
            _install_stub(app)
            editor = app.active_workspace.editor
            for suffix in ("", " ", "  \t"):
                editor.text = "\tselect *" + suffix
                editor.cursor_location = (0, len(editor.text))
                await pilot.press("tab")
                assert editor.text == "\tselect *\n\tfrom "

    asyncio.run(exercise())


def test_table_results_replace_namespace_spacing_and_keywords_keep_spaces() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test() as pilot:
            stub = _install_stub(app)
            stub.schemas[None] = ("sample_schema", "sample_other")
            workspace = app.active_workspace
            editor = workspace.editor
            editor.text = "select * from sample"
            editor.cursor_location = (0, len(editor.text))
            await pilot.press("tab")
            assert workspace.completion_menu.is_open
            stub.table_cache = ("sample_table", "sample_table_two")
            app._receive_completion(
                CompletionResult(app._completion_at_cursor().request, 1, stub.table_cache)
            )
            await pilot.press("tab")
            assert editor.text == "select * from sample_table "
            editor.text = "sel"
            editor.cursor_location = (0, 3)
            await pilot.press("tab")
            assert editor.text == "select "

    asyncio.run(exercise())


@pytest.mark.parametrize("suffix", ["", "name", ".", " "])
def test_namespace_completion_never_appends_space(suffix: str) -> None:
    assert completion_text("sandbox", suffix, append_space=False) == "sandbox"


def test_menu_without_saved_metadata_context_keeps_regular_token_spacing() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test():
            workspace = app.active_workspace
            workspace.editor.text = "sel"
            workspace.editor.cursor_location = (0, 3)
            workspace.completion_menu.open(("select",))
            app._accept_completion()
            assert workspace.editor.text == "select "

    asyncio.run(exercise())


def test_namespace_completion_reuses_existing_dot_and_preserves_following_table() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test() as pilot:
            stub = _install_stub(app)
            stub.schemas[None] = ("sandbox",)
            stub.table_cache = ()
            editor = app.active_workspace.editor
            editor.text = "select * from san.table_name"
            editor.cursor_location = (0, len("select * from san"))
            await pilot.press("tab")
            assert editor.text == "select * from sandbox.table_name"
            assert editor.cursor_location == (0, len("select * from sandbox."))

    asyncio.run(exercise())


@pytest.mark.parametrize("backend", ["gp", "ch", "trino"])
@pytest.mark.parametrize("prefix", ["", "select *\n\n\n"])
@pytest.mark.parametrize("trailing", ["", " ", "  \t"])
def test_select_star_shortcut_uses_trino_catalog_and_preserves_indentation(
    backend: str, prefix: str, trailing: str
) -> None:
    async def exercise() -> None:
        session = FakeSession()
        session.database.backend = backend
        app = SqlExplorerApp(session)
        async with app.run_test() as pilot:
            _install_stub(app)
            editor = app.active_workspace.editor
            editor.text = prefix + "  SELECT" + trailing
            row = prefix.count("\n")
            editor.cursor_location = (row, len("  SELECT" + trailing))
            await pilot.press("tab", "tab")
            expected = prefix + "  SELECT *\n  from " + ("iceberg." if backend == "trino" else "")
            assert editor.text == expected
            assert editor.cursor_location == (row + 1, len(expected.splitlines()[row + 1]))
            editor.action_undo()
            assert editor.text == prefix + "  SELECT * "
            editor.action_undo()
            assert editor.text == prefix + "  SELECT" + trailing

    asyncio.run(exercise())
