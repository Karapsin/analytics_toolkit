from __future__ import annotations

import asyncio

import pytest
from analytics_toolkit.sql_explorer.app import SqlExplorerApp
from analytics_toolkit.sql_explorer.completion import CompletionCoordinator

from tests.sql.explorer.app import FakeSession
from tests.sql.explorer.completion import FakeProvider, _wait_for


@pytest.mark.parametrize("prefix", ["", "o", "order", "orders", "orders_archive"])
@pytest.mark.parametrize("matches", [False, True])
def test_uncached_table_completion_accepts_any_prefix(prefix: str, matches: bool) -> None:
    class Provider(FakeProvider):
        def list_tables(self, **kwargs):
            values = super().list_tables(**kwargs)
            return values if matches else ()

    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        provider = Provider()
        coordinator = CompletionCoordinator("gp", "gp", provider=provider)
        try:
            async with app.run_test() as pilot:
                app._completion.stop()
                app._completion = coordinator
                editor = app.active_workspace.editor
                editor.text = f"SELECT * FROM public.{prefix}"
                editor.cursor_location = (0, len(editor.text))
                await pilot.press("ctrl+space")
                _wait_for(lambda: len(provider.table_calls) == 1)
                await pilot.pause()
                assert provider.table_calls == [(prefix, "public", None)]
                assert app.active_workspace.completion_menu.is_open is matches
                assert app.active_workspace.completion_loading_notice is None
        finally:
            coordinator.stop()
            _wait_for(lambda: coordinator.is_stopped)

    asyncio.run(exercise())
