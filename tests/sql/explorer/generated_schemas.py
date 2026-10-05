from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from analytics_toolkit.sql_explorer.app import SqlExplorerApp
from analytics_toolkit.sql_explorer.background_metadata import BackgroundMetadata
from analytics_toolkit.sql_explorer.completion import (
    CompletionCoordinator,
    CompletionRequest,
    CompletionResult,
    schema_completion_values,
)
from analytics_toolkit.sql_explorer.metadata_store import MetadataStore

from tests.sql.explorer.app import FakeSession
from tests.sql.explorer.completion import FakeProvider, _install_stub, _wait_for

if TYPE_CHECKING:
    from pathlib import Path

GENERATED = "0734e1fdae093d41__839edaf6bb94b99944195a89c3d51ee3ebbd3d__source"


@pytest.mark.parametrize(
    "name",
    [
        GENERATED,
        GENERATED.upper(),
        GENERATED.replace("source", "target"),
        "a" * 16 + "__" + "b" * 16 + "__x",
        "a" * 40 + "__" + "b" * 64 + "__staging_table_1",
        GENERATED + "\ncopy",
    ],
)
def test_generated_schema_names_are_excluded(name: str) -> None:
    assert schema_completion_values((name,)) == ()


@pytest.mark.parametrize(
    "name",
    [
        "public",
        "2024_sales",
        "orders__source",
        "a" * 15 + "__" + "b" * 16 + "__source",
        "a" * 16 + "__" + "b" * 15 + "__source",
        "a" * 16 + "__" + "b" * 16 + "__",
        "g" * 16 + "__" + "b" * 16 + "__source",
        "a" * 16 + "__" + "g" * 16 + "__source",
        "a" * 16 + "_" + "b" * 16 + "__source",
        "project_" + GENERATED,
    ],
)
def test_readable_names_and_near_matches_remain_visible(name: str) -> None:
    assert schema_completion_values((name,)) == (name,)


def test_missing_schema_cache_stays_distinct_from_filtered_empty_cache() -> None:
    assert schema_completion_values(None) is None
    assert schema_completion_values(()) == ()


@pytest.mark.parametrize("backend", ["gp", "ch", "trino"])
@pytest.mark.parametrize("arrival", ["cached", "async"])
def test_schema_menus_filter_cached_and_fresh_names_before_acceptance(
    backend: str, arrival: str
) -> None:
    async def exercise() -> None:
        session = FakeSession()
        session.database.backend = backend
        app = SqlExplorerApp(session)
        async with app.run_test() as pilot:
            stub = _install_stub(app)
            stub.catalogs = ("iceberg",)
            stub.table_cache = ()
            catalog = "iceberg" if backend == "trino" else None
            source = "select * from " + ("iceberg." if catalog else "")
            names = (GENERATED, "sandbox", "sales")
            stub.schemas[catalog] = names if arrival == "cached" else None
            workspace = app.active_workspace
            editor = workspace.editor
            editor.text = source
            editor.cursor_location = (0, len(source))
            await pilot.press("ctrl+space")
            if arrival == "async":
                assert not workspace.completion_menu.is_open
                stub.schemas[catalog] = names
                app._receive_namespace(
                    CompletionResult(CompletionRequest("gp", backend, "schema", ""), 1, names)
                )
            menu = workspace.completion_menu
            assert menu.suggestions == ("sandbox", "sales")
            assert workspace.completion_candidates == ("sandbox", "sales")
            assert stub.schemas[catalog] == names
            await pilot.press("s", "a", "n")
            assert menu.suggestions == ("sandbox",)
            await pilot.press("backspace", "backspace", "backspace")
            assert menu.suggestions == ("sandbox", "sales")
            await pilot.press("enter")
            assert editor.text == source + "sandbox."

            stub.schemas[catalog] = (GENERATED, "public")
            editor.text = source
            editor.cursor_location = (0, len(source))
            await pilot.press("tab")
            assert editor.text == source + "public."
            assert not menu.is_open

            stub.schemas[catalog] = (GENERATED,)
            editor.text = source + GENERATED[:8]
            editor.cursor_location = (0, len(editor.text))
            await pilot.press("tab")
            assert editor.text == source + GENERATED[:8]
            assert not menu.is_open

            stub.table_cache = ("orders",)
            editor.text = source + GENERATED + ".ord"
            editor.cursor_location = (0, len(editor.text))
            assert app._completion_at_cursor().request.schema == GENERATED
            await pilot.press("tab")
            assert editor.text == source + GENERATED + ".orders "

    asyncio.run(exercise())


def test_catalog_table_and_column_suggestions_are_not_filtered() -> None:
    async def exercise() -> None:
        session = FakeSession()
        session.database.backend = "trino"
        app = SqlExplorerApp(session)
        async with app.run_test() as pilot:
            stub = _install_stub(app)
            stub.catalogs = (GENERATED, "iceberg")
            stub.table_cache = ()
            workspace = app.active_workspace
            editor = workspace.editor
            editor.text = "select * from " + GENERATED[:8]
            editor.cursor_location = (0, len(editor.text))
            await pilot.press("tab")
            assert editor.text == "select * from " + GENERATED + "."

            stub.table_cache = (GENERATED, "normal")
            editor.text = "select * from iceberg.public."
            editor.cursor_location = (0, len(editor.text))
            await pilot.press("tab")
            assert workspace.completion_menu.suggestions == stub.table_cache
            await pilot.press("tab")
            assert editor.text == "select * from iceberg.public." + GENERATED + " "

            editor.text = "select  from iceberg.public.orders"
            editor.cursor_location = (0, 7)
            await pilot.press("shift+tab")
            assert workspace.completion_menu.suggestions == stub.table_cache

    asyncio.run(exercise())


def test_saved_generated_schema_and_table_metadata_remain_available(tmp_path: Path) -> None:
    store = MetadataStore(tmp_path, "gp")
    snapshots = {
        ("schema", "iceberg", ""): (GENERATED, "public"),
        ("table", "iceberg", GENERATED): ("orders",),
    }
    store.save(snapshots)
    provider = FakeProvider()
    discovery = BackgroundMetadata("gp", "trino", provider, store, on_error=lambda exc: None)
    coordinator = CompletionCoordinator("gp", "trino", provider=provider, discovery=discovery)
    try:
        names = coordinator.cached_schemas("iceberg")
        assert names == (GENERATED, "public")
        assert schema_completion_values(names) == ("public",)
        request = CompletionRequest(
            "gp", "trino", "table", "ord", catalog="iceberg", schema=GENERATED
        )
        assert coordinator.cached(request) == ("orders",)
        assert store.load() == snapshots
        assert provider.calls == []
    finally:
        coordinator.stop()
        _wait_for(lambda: coordinator.is_stopped)
