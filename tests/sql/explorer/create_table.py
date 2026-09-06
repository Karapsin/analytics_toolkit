from __future__ import annotations

import asyncio
from typing import Any

import pytest
from analytics_toolkit.sql_explorer import runtime
from analytics_toolkit.sql_explorer.app import SqlExplorerApp
from analytics_toolkit.sql_explorer.create_table import creation_options, creation_plan
from analytics_toolkit.sql_explorer.create_table_screen import CreateTableScreen
from textual.widgets import Input, Select, TextArea

from tests.sql.explorer.app import FakeSession
from tests.sql.explorer.runtime import _session


def test_creation_arguments_and_snapshot() -> None:
    draft: dict[str, Any] = {
        "table_name": "sandbox.people",
        "rows": [("id", "BIGINT")],
        "insert_data": True,
    }
    options = creation_options(draft, "gp")
    assert options == {
        "table_name": "sandbox.people",
        "table_schema": {"id": "BIGINT"},
        "insert_data": False,
        "drop_if_exists": False,
        "if_not_exists": False,
    }
    plan = creation_plan(options)
    options["table_name"] = "changed"
    assert plan.options["table_name"] == "sandbox.people"
    draft.update(
        source="from_sql",
        from_sql="select id from source",
        skip_if_exists=True,
        advanced={"source_db": "lake", "retry_cnt": "2"},
    )
    options = creation_options(draft, "gp")
    assert options["sql"] == "select id from source"
    assert options["insert_data"] is True
    assert options["if_not_exists"] is True
    assert options["source_db"] == "lake"
    assert "table_schema" not in options
    assert "retry_cnt" not in options
    assert "timeout_increment" not in options


@pytest.mark.parametrize(
    "draft",
    [
        {},
        {"table_name": "t"},
        {"table_name": "t", "rows": [("id", "")]},
        {"table_name": "t", "rows": [("id", "INT"), ("id", "TEXT")]},
        {"table_name": "t", "source": "from_sql", "from_sql": ""},
        {"table_name": "t", "skip_if_exists": True, "drop_if_exists": True},
    ],
)
def test_invalid_creation_never_produces_a_plan(draft: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match=r"Enter|Each|Duplicate|Choose|Add"):
        creation_options(draft, "gp")


def test_create_dialog_modes_types_and_cancel() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test(size=(110, 45)) as pilot:
            app._handle_command(["create_table"])
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, CreateTableScreen)
            assert screen.db_key == "gp"
            screen.query_one(".create-column-type", Input).focus()
            await pilot.press("b", "i", "enter")
            assert screen.query_one(".create-column-type", Input).value == "BIGINT"
            screen.query_one("#create-table-source", Select).value = "from_sql"
            await pilot.pause()
            assert screen.query_one("#create-from-sql", TextArea).display
            assert not screen.query_one("#create-table-schema").display
            await pilot.press("escape")
            assert not isinstance(app.screen, CreateTableScreen)
            assert app.session.executed == []

    asyncio.run(exercise())


def test_creation_runtime_uses_public_function_and_captured_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(runtime.sql, "create_table", lambda **kwargs: calls.append(kwargs))
    session = _session(monkeypatch, tmp_path)
    result = session.execute(
        creation_plan({"table_name": "sandbox.people", "table_schema": {"id": "BIGINT"}}),
        database=runtime.DatabaseSelection("target", "gp"),
    )
    assert calls[0]["db_key"] == "target"
    assert calls[0]["table_schema"] == {"id": "BIGINT"}
    assert calls[0]["return_metadata"] is True
    assert calls[0]["query_label"].startswith("sql_explorer run=")
    assert "retry_cnt" not in calls[0]
    assert result.dataframe is None
    assert "sandbox.people" in result.status
    assert session.last_query.state == "completed"
    assert session.active_query_label is None


@pytest.mark.parametrize(
    ("advanced", "expected"),
    [
        (
            {"ch_distributed_table": "False", "ch_only_shard": "True"},
            {"ch_distributed_table": False, "ch_only_shard": True},
        ),
        ({"ch_ddl_ready_timeout_seconds": "0"}, {"ch_ddl_ready_timeout_seconds": 0.0}),
    ],
)
def test_advanced_fields_keep_native_types(
    advanced: dict[str, str], expected: dict[str, Any]
) -> None:
    options = creation_options(
        {"table_name": "t", "rows": [("id", "Int64")], "advanced": advanced}, "ch"
    )
    assert options.items() >= expected.items()


@pytest.mark.parametrize(
    "advanced",
    [
        {"ch_ddl_ready_timeout_seconds": "-1"},
        {"ch_ddl_ready_timeout_seconds": "NaN"},
        {"ch_distributed_table": "yes"},
    ],
)
def test_invalid_advanced_values_are_rejected(advanced: dict[str, str]) -> None:
    with pytest.raises(ValueError, match="must be"):
        creation_options({"table_name": "t", "rows": [("id", "Int64")], "advanced": advanced}, "ch")


def test_create_button_submits_and_reopening_retains_values() -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test(size=(110, 45)) as pilot:
            app._command_create_table([])
            await pilot.pause()
            screen = app.screen
            screen.query_one("#create-table-name", Input).value = "sandbox.people"
            screen.query_one(".create-column-name", Input).value = "id"
            screen.query_one(".create-column-type", Input).value = "BIGINT"
            await pilot.pause()
            await pilot.click("#create-table-submit")
            await pilot.pause()
            assert app.session.executed[0].options["table_name"] == "sandbox.people"
            app._command_create_table([])
            await pilot.pause()
            assert app.screen.query_one("#create-table-name", Input).value == "sandbox.people"
            await pilot.press("escape")

    asyncio.run(exercise())


@pytest.mark.parametrize("value", ["[]", "null", "1", "{broken"])
def test_partition_mapping_requires_json_object(value: str) -> None:
    with pytest.raises(ValueError, match=r"JSON object|Expecting property name"):
        creation_options(
            {"table_name": "t", "rows": [("id", "BIGINT")], "advanced": {"gp_partitions": value}},
            "gp",
        )


def test_partition_mapping_preserves_structured_arguments() -> None:
    options = creation_options(
        {
            "table_name": "t",
            "rows": [("id", "BIGINT")],
            "advanced": {"gp_partitions": '{"partition_type": "RANGE"}'},
        },
        "gp",
    )
    assert options["gp_partitions"] == {"partition_type": "RANGE"}


def test_creation_completion_only_invalidates_matching_database_tabs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> None:
        app = SqlExplorerApp(FakeSession())
        async with app.run_test() as pilot:
            first = app.active_workspace
            app.action_new_tab()
            await pilot.pause()
            second = app.active_workspace
            second.session.database = runtime.DatabaseSelection("other", "gp")
            second.completion_requested_text = "pending elsewhere"
            monkeypatch.setattr(app._completion_pool, "coordinator_for", lambda _: None)
            job = app._query_scheduler.enqueue(
                first.tab_id,
                creation_plan({"table_name": "t", "table_schema": {"id": "BIGINT"}}),
                first.session.database,
            )
            assert app._query_scheduler.take_startable() == (job,)
            app._finish_query_job(job, None, ValueError("creation failed"))
            assert first.completion_requested_text is None
            assert second.completion_requested_text == "pending elsewhere"
            assert "creation failed" in str(first.result_message.render())

    asyncio.run(exercise())
