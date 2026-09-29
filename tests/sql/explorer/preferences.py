from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from analytics_toolkit.sql_explorer import preferences, settings
from analytics_toolkit.sql_explorer.app import SqlExplorerApp
from analytics_toolkit.sql_explorer.runtime import DatabaseSelection
from analytics_toolkit.sql_explorer.settings import (
    ExplorerSettings,
    explorer_settings_path,
    load_local_settings,
    load_settings,
    save_settings,
)
from analytics_toolkit.sql_explorer.statements import build_execution_plan

from tests.sql.explorer.app import FakeSession

if TYPE_CHECKING:
    from pathlib import Path


def test_migrate_existing_preferences_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "config"))
    expected = ExplorerSettings(run_binding="f9", confirm_mutations=False)
    save_settings(expected)
    target = tmp_path / ".sql_explorer" / "settings.json"
    assert load_local_settings(target).settings == expected
    changed = replace(expected, results_orientation="vertical", keyboard_mode=True)
    save_settings(changed, target)
    assert load_local_settings(target).settings == changed
    assert load_settings(explorer_settings_path()).settings == expected


def test_ui_choices_restore_and_existing_tabs_keep_layout(tmp_path: Path) -> None:
    async def exercise() -> None:
        session = FakeSession()
        session.settings_path = tmp_path / "settings.json"
        app = SqlExplorerApp(session)
        async with app.run_test(size=(120, 40)) as pilot:
            first = app.active_workspace
            await pilot.press("ctrl+t")
            app._command_results(["switch"])
            app._command_results(["expand", "8"])
            app._command_keyboard(["on"])
            assert first.results_orientation == "horizontal"
            await pilot.press("ctrl+t")
            assert app.active_workspace.results_orientation == "vertical"
            assert app.active_workspace.result_sizes["vertical"] is not None
        restored = FakeSession()
        restored.settings = load_settings(session.settings_path).settings
        again = SqlExplorerApp(restored)
        async with again.run_test(size=(50, 20)):
            assert again.keyboard_mode
            assert again.active_workspace.results_orientation == "vertical"
            assert not again.active_workspace.results_open
            assert again.active_workspace.result_sizes["vertical"] is not None

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("results_orientation", "diagonal"),
        ("horizontal_size", -1),
        ("vertical_size", True),
        ("keyboard_mode", "yes"),
    ],
)
def test_invalid_visual_preferences_are_ignored(tmp_path: Path, field: str, value: object) -> None:
    path = tmp_path / "settings.json"
    save_settings(ExplorerSettings(), path)
    raw = json.loads(path.read_text())
    raw[field] = value
    path.write_text(json.dumps(raw))
    loaded = load_settings(path)
    assert loaded.settings == ExplorerSettings()
    assert loaded.warning


def test_failed_migration_keeps_preferences(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path))

    def fail_save(*args):
        message = "read only"
        raise OSError(message)

    monkeypatch.setattr(settings, "save_settings", fail_save)
    loaded = load_local_settings(tmp_path / "missing" / "settings.json")
    assert loaded.settings == ExplorerSettings()
    assert "read only" in loaded.warning


@pytest.mark.parametrize(
    ("sql_text", "expected"),
    [
        ("-- comment\n CREATE TABLE demo (id int)", True),
        ("ALTER TABLE demo ADD COLUMN name text", True),
        ("DROP TABLE demo", True),
        ("SELECT * FROM demo", False),
        ("INSERT INTO demo VALUES (1)", False),
        ("SELECT 1 INTO demo", True),
    ],
)
def test_ddl_marks_metadata_for_refresh(sql_text: str, expected: bool) -> None:
    assert build_execution_plan(sql_text, "gp").changes_metadata is expected


def test_failed_ui_save_keeps_current_preferences_and_reports_notice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_save(*args):
        message = "read only"
        raise OSError(message)

    monkeypatch.setattr(preferences, "save_settings", fail_save)

    async def exercise() -> None:
        session = FakeSession()
        session.settings_path = tmp_path / "settings.json"
        app = SqlExplorerApp(session)
        async with app.run_test():
            notices: list[str] = []
            monkeypatch.setattr(
                app, "_set_notice", lambda notice, workspace=None: notices.append(notice)
            )
            app._command_results(["switch"])
            assert session.settings.results_orientation == "vertical"
            assert any("read only" in notice for notice in notices)

    asyncio.run(exercise())


@pytest.mark.parametrize("coordinator_present", [False, True])
def test_ddl_completion_leaves_other_connections_untouched(
    monkeypatch: pytest.MonkeyPatch,
    coordinator_present: bool,
) -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test() as pilot:
            first = app.active_workspace
            await pilot.press("ctrl+t")
            other = app.active_workspace
            other.session.database = DatabaseSelection("other", "gp")
            other.completion_candidates = ("keep",)
            first.completion_candidates = ("discard",)
            invalidations: list[str] = []
            coordinator = SimpleNamespace(invalidate_tables=lambda: invalidations.append("gp"))
            monkeypatch.setattr(
                app._completion_pool,
                "coordinator_for",
                lambda key: coordinator if coordinator_present else None,
            )
            plan = build_execution_plan("DROP TABLE old_orders", "gp")
            job = app._query_scheduler.enqueue(first.tab_id, plan, first.session.database)
            assert app._query_scheduler.take_startable() == (job,)
            app._finish_query_job(job, None, None)
            assert first.completion_candidates == ()
            assert other.completion_candidates == ("keep",)
            assert invalidations == (["gp"] if coordinator_present else [])

    asyncio.run(exercise())
