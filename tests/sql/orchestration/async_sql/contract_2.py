from __future__ import annotations

from tests.sql._support.async_sql import (
    Any,
    async_module,
    asyncio,
    inspect,
    named_tasks,
    pd,
    pytest,
    time_print,
)


def test_async_sql_start_comment_prefixes_sql_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, dict[str, Any]] = {}

    def record(task_type: str, result_field: str | None):
        def fake_operation(**kwargs: Any) -> Any:
            calls[task_type] = kwargs
            if result_field is None:
                return None
            return kwargs[result_field]

        return fake_operation

    monkeypatch.setattr(async_module, "read_sql", record("read", "query"))
    monkeypatch.setattr(async_module, "execute_sql", record("execute", None))
    monkeypatch.setattr(
        async_module,
        "execute_read",
        record("execute_read", "query"),
    )
    monkeypatch.setattr(
        async_module,
        "transfer_table",
        record("transfer", "from_sql"),
    )

    result = async_module.async_sql(
        named_tasks(
            {
                "read_users": {
                    "type": "read",
                    "db_key": "gp",
                    "query": "select * from users",
                },
                "refresh_table": {
                    "type": "execute",
                    "db_key": "gp",
                    "query": "truncate table sandbox.target",
                },
                "prepare_and_read": {
                    "type": "execute_read",
                    "db_key": "trino",
                    "query": "create table tmp as select 1; select * from tmp",
                },
                "copy_table": {
                    "type": "transfer",
                    "from_db": "gp",
                    "to_db": "trino",
                    "from_sql": "select * from source",
                    "to_table": "sandbox.copy",
                },
            }
        ),
        concurrency=1,
        start_comment="/* async batch */  \n",
        progress=False,
    )

    assert result["read_users"] == "/* async batch */\nselect * from users"
    assert result["refresh_table"] == "success"
    assert (
        result["prepare_and_read"]
        == "/* async batch */\ncreate table tmp as select 1; select * from tmp"
    )
    assert result["copy_table"] == "/* async batch */\nselect * from source"
    assert calls["read"]["query"] == "/* async batch */\nselect * from users"
    assert calls["execute"]["query"] == "/* async batch */\ntruncate table sandbox.target"
    assert (
        calls["execute_read"]["query"]
        == "/* async batch */\ncreate table tmp as select 1; select * from tmp"
    )
    assert calls["transfer"]["from_sql"] == "/* async batch */\nselect * from source"


def test_async_sql_sync_task_dispatch_uses_callable_registry() -> None:
    assert set(async_module._SYNC_TASK_RUNNERS) == {
        "read",
        "execute",
        "execute_read",
        "load_df",
        "transfer",
    }
    assert all(callable(runner) for runner in async_module._SYNC_TASK_RUNNERS.values())
    assert {
        "execute",
        "execute_read",
        "load_df",
        "transfer",
    } == async_module._PROGRESS_TASK_TYPES
    source = inspect.getsource(async_module._run_sync_task)
    assert "if task_type == " not in source


def test_async_sql_task_context_task_id_is_included_in_task_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    expected_rows = pd.DataFrame({"id": [1]})

    def fake_read_sql(
        *,
        db_key: str,
        query: str,
        print_queries: bool = False,
    ) -> pd.DataFrame:
        time_print("task output from read_sql")
        return expected_rows

    monkeypatch.setattr(async_module, "read_sql", fake_read_sql)

    result = async_module.async_sql(
        [
            {
                "name": "copy_table",
                "type": "read",
                "db_key": "gp",
                "query": "select * from source limit 1",
            }
        ],
        concurrency=1,
    )

    assert list(result) == ["copy_table"]
    pd.testing.assert_frame_equal(result["copy_table"], expected_rows)
    assert "[task_id=copy_table]" in capsys.readouterr().out


def test_async_sql_task_start_comment_overrides_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_read_sql(**kwargs: Any) -> str:
        return kwargs["query"]

    monkeypatch.setattr(async_module, "read_sql", fake_read_sql)

    result = async_module.async_sql(
        named_tasks(
            {
                "default": {
                    "type": "read",
                    "db_key": "gp",
                    "query": "select default",
                },
                "override": {
                    "type": "read",
                    "db_key": "gp",
                    "query": "select override",
                    "start_comment": "  -- task override  ",
                },
                "blank": {
                    "type": "read",
                    "db_key": "gp",
                    "query": "select blank",
                    "start_comment": "   ",
                },
                "none": {
                    "type": "read",
                    "db_key": "gp",
                    "query": "select none",
                    "start_comment": None,
                },
            }
        ),
        concurrency=1,
        start_comment="-- default",
        progress=False,
    )

    assert result == {
        "default": "-- default\nselect default",
        "override": "  -- task override\nselect override",
        "blank": "select blank",
        "none": "select none",
    }


def test_async_sql_uses_generated_names_for_unnamed_task_sequence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_execute_sql(**kwargs: Any) -> str:
        calls.append(kwargs)
        return kwargs["query"]

    monkeypatch.setattr(async_module, "execute_sql", fake_execute_sql)

    result = async_module.async_sql(
        [
            {
                "type": "execute",
                "db_key": "gp",
                "query": "insert into target select 1",
            },
            {
                "type": "execute",
                "db_key": "gp",
                "query": "insert into target select 2",
            },
        ],
        concurrency=1,
        progress=True,
    )

    assert result == {
        "task_0": "insert into target select 1",
        "task_1": "insert into target select 2",
    }
    assert calls == [
        {
            "db_key": "gp",
            "query": "insert into target select 1",
            "progress": False,
        },
        {
            "db_key": "gp",
            "query": "insert into target select 2",
            "progress": False,
        },
    ]


def test_async_thread_runner_preserves_base_exception() -> None:
    message = "stop"
    error = KeyboardInterrupt(message)

    async def fail() -> dict[str, Any]:
        raise error

    with pytest.raises(KeyboardInterrupt) as caught:
        async_module._run_coroutine_sync_in_thread(fail)
    assert caught.value is error


def test_async_to_thread_fallback_copies_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = async_module.contextvars.ContextVar("test_marker", default="missing")
    marker.set("copied")
    monkeypatch.setattr(async_module.asyncio, "to_thread", None, raising=False)

    assert asyncio.run(async_module._to_thread(marker.get)) == "copied"
