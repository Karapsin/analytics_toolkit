from __future__ import annotations

from tests.sql._support.async_sql import (
    Any,
    async_module,
    asyncio,
    named_tasks,
    pd,
    pytest,
    sql_module,
    threading,
    time,
)


@pytest.mark.parametrize("start_comment", [None, "   "])
def test_async_sql_blank_and_none_start_comment_are_noops(
    monkeypatch: pytest.MonkeyPatch,
    start_comment: str | None,
) -> None:
    def fake_read_sql(**kwargs: Any) -> str:
        return kwargs["query"]

    monkeypatch.setattr(async_module, "read_sql", fake_read_sql)

    result = async_module.async_sql(
        [
            {
                "type": "read",
                "db_key": "gp",
                "query": "select 1",
            }
        ],
        start_comment=start_comment,
        progress=False,
    )

    assert result == {"task_0": "select 1"}


def test_async_sql_dispatches_supported_task_types_and_preserves_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    read_result = pd.DataFrame({"value": [1]})
    execute_read_result = pd.DataFrame({"value": [2]})
    load_result = 3
    transfer_result = 4
    df = pd.DataFrame({"id": [1]})

    def fake_read_sql(
        *,
        db_key: str,
        query: str,
        print_queries: bool = False,
    ) -> pd.DataFrame:
        calls.append(
            (
                "read",
                {
                    "db_key": db_key,
                    "query": query,
                    "print_queries": print_queries,
                },
            )
        )
        return read_result

    def fake_execute_sql(
        *,
        db_key: str,
        query: str,
        gp_break_query: bool = False,
        gp_commit_each_statement: bool = False,
        progress: bool = False,
    ) -> None:
        calls.append(
            (
                "execute",
                {
                    "db_key": db_key,
                    "query": query,
                    "gp_break_query": gp_break_query,
                    "gp_commit_each_statement": gp_commit_each_statement,
                    "progress": progress,
                },
            )
        )

    def fake_execute_read(
        *,
        db_key: str,
        query: str,
        progress: bool = False,
    ) -> pd.DataFrame:
        calls.append(
            (
                "execute_read",
                {"db_key": db_key, "query": query, "progress": progress},
            )
        )
        return execute_read_result

    def fake_load_df(
        *,
        db_key: str,
        destination_table: str,
        df: pd.DataFrame,
        append: bool = False,
        order_by: Any = None,
        progress: bool = False,
    ) -> int:
        calls.append(
            (
                "load_df",
                {
                    "db_key": db_key,
                    "destination_table": destination_table,
                    "df": df,
                    "append": append,
                    "order_by": order_by,
                    "progress": progress,
                },
            )
        )
        return load_result

    def fake_transfer_table(
        *,
        from_db: str,
        to_db: str,
        from_sql: str,
        to_table: str,
        batch_size: int = 100000,
        estimate_total_rows: bool = False,
        progress: bool = False,
    ) -> int:
        calls.append(
            (
                "transfer",
                {
                    "from_db": from_db,
                    "to_db": to_db,
                    "from_sql": from_sql,
                    "to_table": to_table,
                    "batch_size": batch_size,
                    "estimate_total_rows": estimate_total_rows,
                    "progress": progress,
                },
            )
        )
        return transfer_result

    monkeypatch.setattr(async_module, "read_sql", fake_read_sql)
    monkeypatch.setattr(async_module, "execute_sql", fake_execute_sql)
    monkeypatch.setattr(async_module, "execute_read", fake_execute_read)
    monkeypatch.setattr(async_module, "load_df", fake_load_df)
    monkeypatch.setattr(async_module, "transfer_table", fake_transfer_table)

    tasks = named_tasks(
        {
            "read_users": {
                "type": "read",
                "db_key": "gp",
                "query": "select * from users",
                "print_queries": False,
            },
            "refresh_table": {
                "type": "execute",
                "db_key": "gp",
                "query": "truncate table sandbox.target",
                "gp_break_query": True,
                "gp_commit_each_statement": True,
            },
            "prepare_and_read": {
                "type": "execute_read",
                "db_key": "trino",
                "query": "create table tmp as select 1; select * from tmp",
            },
            "load_batch": {
                "type": "load_df",
                "db_key": "ch",
                "destination_table": "sandbox.batch",
                "df": df,
                "append": True,
                "order_by": ["id"],
            },
            "copy_table": {
                "type": "transfer",
                "from_db": "gp",
                "to_db": "trino",
                "from_sql": "select * from source",
                "to_table": "sandbox.copy",
                "batch_size": 10,
                "estimate_total_rows": True,
            },
        }
    )

    result = async_module.async_sql(tasks, concurrency=3)

    assert list(result) == [
        "read_users",
        "refresh_table",
        "prepare_and_read",
        "load_batch",
        "copy_table",
    ]
    pd.testing.assert_frame_equal(result["read_users"], read_result)
    assert result["refresh_table"] == "success"
    pd.testing.assert_frame_equal(result["prepare_and_read"], execute_read_result)
    assert result["load_batch"] == load_result
    assert result["copy_table"] == transfer_result

    calls_by_type = {task_type: kwargs for task_type, kwargs in calls}
    assert calls_by_type["read"] == {
        "db_key": "gp",
        "query": "select * from users",
        "print_queries": False,
    }
    assert calls_by_type["execute"] == {
        "db_key": "gp",
        "query": "truncate table sandbox.target",
        "gp_break_query": True,
        "gp_commit_each_statement": True,
        "progress": False,
    }
    assert calls_by_type["execute_read"] == {
        "db_key": "trino",
        "query": "create table tmp as select 1; select * from tmp",
        "progress": False,
    }
    load_kwargs = calls_by_type["load_df"]
    assert load_kwargs["df"] is df
    assert {key: value for key, value in load_kwargs.items() if key != "df"} == {
        "db_key": "ch",
        "destination_table": "sandbox.batch",
        "append": True,
        "order_by": ["id"],
        "progress": False,
    }
    assert calls_by_type["transfer"] == {
        "from_db": "gp",
        "to_db": "trino",
        "from_sql": "select * from source",
        "to_table": "sandbox.copy",
        "batch_size": 10,
        "estimate_total_rows": True,
        "progress": False,
    }


def test_async_sql_fail_fast_false_returns_exceptions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = RuntimeError("read failed")

    def fake_read_sql(**kwargs: Any) -> str:
        if kwargs["query"] == "select broken":
            raise error
        return kwargs["query"]

    def fake_execute_sql(**kwargs: Any) -> None:
        return None

    monkeypatch.setattr(async_module, "read_sql", fake_read_sql)
    monkeypatch.setattr(async_module, "execute_sql", fake_execute_sql)

    result = async_module.async_sql(
        named_tasks(
            {
                "ok": {
                    "type": "read",
                    "db_key": "gp",
                    "query": "select ok",
                },
                "broken": {
                    "type": "read",
                    "db_key": "gp",
                    "query": "select broken",
                },
                "write_ok": {
                    "type": "execute",
                    "db_key": "gp",
                    "query": "truncate table sandbox.target",
                },
            }
        ),
        fail_fast=False,
    )

    assert result["ok"] == "select ok"
    assert result["broken"] == str(error)
    assert result["write_ok"] == "success"


def test_async_sql_fail_fast_false_returns_pipeline_exception() -> None:
    error = RuntimeError("pipeline failed")

    def broken_step(context: Any) -> None:
        raise error

    result = async_module.async_sql(
        [
            {
                "name": "pipeline",
                "type": "custom_sql_pipeline",
                "steps": [broken_step],
            }
        ],
        fail_fast=False,
    )

    assert result["pipeline"] == str(error)


def test_async_sql_fail_fast_raises_first_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = RuntimeError("read failed")

    def fake_read_sql(**kwargs: Any) -> str:
        raise error

    monkeypatch.setattr(async_module, "read_sql", fake_read_sql)

    tasks = named_tasks(
        {
            "broken": {
                "type": "read",
                "db_key": "gp",
                "query": "select broken",
            },
            "also_broken": {
                "type": "read",
                "db_key": "gp",
                "query": "select also_broken",
            },
        }
    )

    with pytest.raises(RuntimeError) as exc_info:
        async_module.async_sql(tasks, concurrency=1, fail_fast=True)

    assert exc_info.value is error


def test_async_sql_is_exported() -> None:
    assert sql_module.async_sql is async_module.async_sql


def test_async_sql_pipeline_can_run_nested_sync_async_sql(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_read_sql(**kwargs: Any) -> str:
        return kwargs["query"]

    monkeypatch.setattr(async_module, "read_sql", fake_read_sql)

    def nested_batch(context: Any) -> dict[str, Any]:
        return async_module.async_sql(
            named_tasks(
                {
                    "a": {
                        "type": "read",
                        "db_key": "gp",
                        "query": f"{context.task_name}:a",
                    },
                    "b": {
                        "type": "read",
                        "db_key": "gp",
                        "query": f"{context.task_name}:b",
                    },
                }
            ),
            concurrency=2,
        )

    result = async_module.async_sql(
        [
            {
                "name": "pipeline",
                "type": "custom_sql_pipeline",
                "steps": [nested_batch],
            }
        ]
    )

    assert result["pipeline"] == {"a": "pipeline:a", "b": "pipeline:b"}


def test_async_sql_pipeline_runs_steps_sequentially_and_returns_last_result() -> None:
    observations: list[tuple[str, int, list[Any], Any]] = []

    def first_step(context: Any) -> str:
        observations.append(
            (
                context.task_name,
                context.step_index,
                list(context.results),
                context.last_result,
            )
        )
        return "first"

    async def second_step(context: Any) -> str:
        await asyncio.sleep(0)
        observations.append(
            (
                context.task_name,
                context.step_index,
                list(context.results),
                context.last_result,
            )
        )
        return f"{context.last_result}:second"

    result = async_module.async_sql(
        [
            {
                "name": "pipeline",
                "type": "custom_sql_pipeline",
                "steps": [first_step, second_step],
            }
        ]
    )

    assert result["pipeline"] == "first:second"
    assert observations == [
        ("pipeline", 0, [], None),
        ("pipeline", 1, ["first"], "first"),
    ]


def test_async_sql_pipeline_stops_on_first_step_exception() -> None:
    error = RuntimeError("pipeline failed")
    calls: list[str] = []

    def broken_step(context: Any) -> None:
        calls.append("broken")
        raise error

    def skipped_step(context: Any) -> None:
        calls.append("skipped")

    with pytest.raises(RuntimeError) as exc_info:
        async_module.async_sql(
            [
                {
                    "name": "pipeline",
                    "type": "custom_sql_pipeline",
                    "steps": [broken_step, skipped_step],
                }
            ]
        )

    assert exc_info.value is error
    assert calls == ["broken"]


def test_async_sql_runs_from_inside_existing_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_execute_sql(**kwargs: Any) -> None:
        return None

    monkeypatch.setattr(async_module, "execute_sql", fake_execute_sql)

    async def call_sync_api() -> dict[str, Any]:
        return async_module.async_sql(
            [
                {
                    "type": "execute",
                    "db_key": "gp",
                    "query": "insert into target select 1",
                }
            ],
            concurrency=1,
        )

    assert asyncio.run(call_sync_api()) == {"task_0": "success"}


def test_async_sql_soft_cap_limits_top_level_worker_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock = threading.Lock()
    active_workers = 0
    max_active_workers = 0

    def fake_read_sql(**kwargs: Any) -> str:
        nonlocal active_workers, max_active_workers
        with lock:
            active_workers += 1
            max_active_workers = max(max_active_workers, active_workers)
        time.sleep(0.1)
        with lock:
            active_workers -= 1
        return kwargs["query"]

    monkeypatch.setattr(async_module, "read_sql", fake_read_sql)

    tasks = named_tasks(
        {
            f"read_{index}": {
                "type": "read",
                "db_key": "gp",
                "query": f"select {index}",
            }
            for index in range(6)
        }
    )

    result = async_module.async_sql(tasks, concurrency=6, soft_concurrency_cap=2)

    assert list(result) == [f"read_{index}" for index in range(6)]
    assert max_active_workers == 2


def test_async_sql_start_comment_does_not_change_load_df_or_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    load_calls: list[dict[str, Any]] = []

    def fake_load_df(**kwargs: Any) -> int:
        load_calls.append(kwargs)
        return 1

    def pipeline_step(context: Any) -> str:
        return context.task_name

    monkeypatch.setattr(async_module, "load_df", fake_load_df)

    df = pd.DataFrame({"id": [1]})
    result = async_module.async_sql(
        named_tasks(
            {
                "load_batch": {
                    "type": "load_df",
                    "db_key": "ch",
                    "destination_table": "sandbox.batch",
                    "df": df,
                    "start_comment": "-- ignored",
                },
                "pipeline": {
                    "type": "custom_sql_pipeline",
                    "steps": [pipeline_step],
                },
            }
        ),
        concurrency=1,
        start_comment="-- default",
        progress=False,
    )

    assert result == {"load_batch": 1, "pipeline": "pipeline"}
    assert len(load_calls) == 1
    load_kwargs = load_calls[0]
    assert load_kwargs["df"] is df
    assert {key: value for key, value in load_kwargs.items() if key != "df"} == {
        "db_key": "ch",
        "destination_table": "sandbox.batch",
        "progress": False,
    }
