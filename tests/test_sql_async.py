from __future__ import annotations

import asyncio
import importlib
import inspect
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

async_module = importlib.import_module("analytics_toolkit.sql.orchestration.async_sql")
sql_module = importlib.import_module("analytics_toolkit.sql")


def named_tasks(tasks: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"name": name, **spec} for name, spec in tasks.items()]


def sql_task_spec(task_type: str) -> dict[str, Any]:
    if task_type == "read":
        return {"type": "read", "db_key": "gp", "query": "select 1"}
    if task_type == "execute":
        return {"type": "execute", "db_key": "gp", "query": "select 1"}
    if task_type == "execute_read":
        return {"type": "execute_read", "db_key": "gp", "query": "select 1"}
    if task_type == "load_df":
        return {
            "type": "load_df",
            "db_key": "gp",
            "destination_table": "sandbox.target",
            "df": pd.DataFrame({"id": [1]}),
        }
    if task_type == "transfer":
        return {
            "type": "transfer",
            "from_db": "gp",
            "to_db": "trino",
            "from_sql": "select 1",
            "to_table": "sandbox.target",
        }
    raise ValueError(f"Unsupported task type for test: {task_type}")


def test_async_sql_is_exported() -> None:
    assert sql_module.async_sql is async_module.async_sql


def test_async_sql_sync_task_dispatch_uses_callable_registry() -> None:
    assert set(async_module._SYNC_TASK_RUNNERS) == {
        "read",
        "execute",
        "execute_read",
        "load_df",
        "transfer",
    }
    assert all(callable(runner) for runner in async_module._SYNC_TASK_RUNNERS.values())
    assert async_module._PROGRESS_TASK_TYPES == {
        "execute",
        "execute_read",
        "load_df",
        "transfer",
    }
    source = inspect.getsource(async_module._run_sync_task)
    assert "if task_type == " not in source


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


@pytest.mark.parametrize(
    "task_type",
    ["read", "execute", "execute_read", "load_df", "transfer"],
)
@pytest.mark.parametrize(
    "forbidden_field",
    ["connection", "connection_type", "connection_key", "backend"],
)
def test_async_sql_rejects_removed_connection_task_arguments(
    task_type: str,
    forbidden_field: str,
) -> None:
    task = sql_task_spec(task_type)
    task[forbidden_field] = "gp"

    with pytest.raises(
        ValueError,
        match=rf"unsupported SQL task argument.*{forbidden_field}",
    ):
        async_module.async_sql([task], concurrency=1, progress=False)


def test_async_sql_suppresses_builtin_inner_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, dict[str, Any]] = {}
    df = pd.DataFrame({"id": [1]})

    def fake_execute_sql(**kwargs: Any) -> None:
        calls["execute"] = kwargs
        return None

    def fake_execute_read(**kwargs: Any) -> pd.DataFrame:
        calls["execute_read"] = kwargs
        return pd.DataFrame({"id": [1]})

    def fake_load_df(**kwargs: Any) -> int:
        calls["load_df"] = kwargs
        return 1

    def fake_transfer_table(**kwargs: Any) -> int:
        calls["transfer"] = kwargs
        return 2

    monkeypatch.setattr(async_module, "execute_sql", fake_execute_sql)
    monkeypatch.setattr(async_module, "execute_read", fake_execute_read)
    monkeypatch.setattr(async_module, "load_df", fake_load_df)
    monkeypatch.setattr(async_module, "transfer_table", fake_transfer_table)

    execute_spec = {
        "name": "refresh",
        "type": "execute",
        "db_key": "ch",
        "query": "select 1; select 2",
        "progress": True,
    }
    execute_read_spec = {
        "name": "prepare",
        "type": "execute_read",
        "db_key": "ch",
        "query": "select 1; select 2",
        "progress": True,
    }
    load_spec = {
        "name": "load_batch",
        "type": "load_df",
        "db_key": "gp",
        "destination_table": "sandbox.batch",
        "df": df,
        "progress": True,
    }
    transfer_spec = {
        "name": "copy_table",
        "type": "transfer",
        "from_db": "gp",
        "to_db": "trino",
        "from_sql": "select * from source",
        "to_table": "sandbox.copy",
        "progress": True,
    }

    result = async_module.async_sql(
        [execute_spec, execute_read_spec, load_spec, transfer_spec],
        concurrency=1,
        progress=False,
    )

    assert result["refresh"] == "success"
    pd.testing.assert_frame_equal(result["prepare"], pd.DataFrame({"id": [1]}))
    assert result["load_batch"] == 1
    assert result["copy_table"] == 2
    assert calls["execute"]["progress"] is False
    assert calls["execute_read"]["progress"] is False
    assert calls["load_df"]["progress"] is False
    assert calls["transfer"]["progress"] is False
    assert execute_spec["progress"] is True
    assert execute_read_spec["progress"] is True
    assert load_spec["progress"] is True
    assert transfer_spec["progress"] is True


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


def test_async_sql_rejects_removed_random_sleep_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_execute_read(
        *,
        db_key: str,
        query: str,
        progress: bool = False,
    ) -> pd.DataFrame:
        return pd.DataFrame({"query": [query], "db_key": [db_key]})

    monkeypatch.setattr(async_module, "execute_read", fake_execute_read)

    with pytest.raises(TypeError, match="random_sleep_seconds"):
        async_module.async_sql(
            [
                {
                    "type": "execute_read",
                    "db_key": "trino",
                    "query": "select 1",
                    "random_sleep_seconds": None,
                }
            ],
            concurrency=1,
            progress=False,
        )


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


@pytest.mark.parametrize(
    "kwargs",
    [
        {"start_comment": 1},
        {"start_comment": True},
    ],
)
def test_async_sql_rejects_non_string_top_level_start_comment(
    kwargs: dict[str, Any],
) -> None:
    with pytest.raises(ValueError, match="start_comment"):
        async_module.async_sql(
            [
                {
                    "type": "read",
                    "db_key": "gp",
                    "query": "select 1",
                }
            ],
            **kwargs,
        )


@pytest.mark.parametrize("start_comment", [1, True])
def test_async_sql_rejects_non_string_task_start_comment(
    start_comment: Any,
) -> None:
    with pytest.raises(ValueError, match="start_comment"):
        async_module.async_sql(
            [
                {
                    "type": "read",
                    "db_key": "gp",
                    "query": "select 1",
                    "start_comment": start_comment,
                }
            ]
        )


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


def test_async_sql_updates_progress_bar(monkeypatch: pytest.MonkeyPatch) -> None:
    progress_bars: list[Any] = []

    class FakeTqdm:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs
            self.updates: list[int] = []
            self.closed = False
            progress_bars.append(self)

        def update(self, value: int) -> None:
            self.updates.append(value)

        def close(self) -> None:
            self.closed = True

    def fake_execute_sql(**kwargs: Any) -> None:
        return None

    monkeypatch.setattr(async_module, "tqdm", FakeTqdm)
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
        "task_0": "success",
        "task_1": "success",
    }
    assert len(progress_bars) == 1
    progress_bar = progress_bars[0]
    assert progress_bar.kwargs == {
        "total": 2,
        "desc": "async_sql tasks",
        "unit": "task",
        "disable": False,
    }
    assert progress_bar.updates == [1, 1]
    assert progress_bar.closed


def test_async_sql_concurrency_limits_active_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock = threading.Lock()
    active_tasks = 0
    max_active_tasks = 0

    def fake_read_sql(**kwargs: Any) -> str:
        nonlocal active_tasks, max_active_tasks
        with lock:
            active_tasks += 1
            max_active_tasks = max(max_active_tasks, active_tasks)
        time.sleep(0.1)
        with lock:
            active_tasks -= 1
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

    result = async_module.async_sql(tasks, concurrency=2)

    assert list(result) == [f"read_{index}" for index in range(6)]
    assert max_active_tasks == 2


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


def test_async_sql_hard_cap_rejects_unthrottled_effective_concurrency() -> None:
    tasks = [
        {
            "type": "read",
            "db_key": "gp",
            "query": f"select {index}",
        }
        for index in range(11)
    ]

    with pytest.raises(
        ValueError,
        match=(
            "effective concurrency exceeds hard_concurrency_cap.*"
            "soft_concurrency_cap"
        ),
    ):
        async_module.async_sql(tasks, concurrency=11)


def test_async_sql_lower_soft_cap_avoids_hard_cap_error(
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

    tasks = [
        {
            "type": "read",
            "db_key": "gp",
            "query": f"select {index}",
        }
        for index in range(11)
    ]

    result = async_module.async_sql(
        tasks,
        concurrency=11,
        soft_concurrency_cap=5,
        hard_concurrency_cap=10,
    )

    assert list(result) == [f"task_{index}" for index in range(11)]
    assert max_active_workers == 5


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


def test_async_sql_fail_fast_true_prints_failed_transfer_sql(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    error = RuntimeError("transfer failed")

    def fake_transfer_table(**kwargs: Any) -> str:
        raise error

    monkeypatch.setattr(async_module, "transfer_table", fake_transfer_table)

    with pytest.raises(RuntimeError) as exc_info:
        async_module.async_sql(
            [
                {
                    "name": "copy_table",
                    "type": "transfer",
                    "from_db": "gp",
                    "to_db": "trino",
                    "from_sql": "select * from source_table",
                    "to_table": "sandbox.copy",
                }
            ],
            concurrency=1,
            fail_fast=True,
            progress=False,
        )

    assert exc_info.value is error
    assert error.analytics_toolkit_sql_task_name == "copy_table"  # type: ignore[attr-defined]
    assert error.analytics_toolkit_sql_field == "from_sql"  # type: ignore[attr-defined]
    assert error.analytics_toolkit_sql_query == "select * from source_table"  # type: ignore[attr-defined]
    output = capsys.readouterr().out
    assert "SQL task 'copy_table' (transfer) failed while running from_sql" in output
    assert "from_sql:\nselect * from source_table" in output


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


def test_async_sql_fail_fast_false_prints_failed_task_query(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    error = RuntimeError("read failed")

    def fake_read_sql(**kwargs: Any) -> str:
        raise error

    monkeypatch.setattr(async_module, "read_sql", fake_read_sql)

    result = async_module.async_sql(
        [
            {
                "name": "broken",
                "type": "read",
                "db_key": "gp",
                "query": "select * from broken_table",
            }
        ],
        fail_fast=False,
        progress=False,
    )

    assert result["broken"] == str(error)
    output = capsys.readouterr().out
    assert "SQL task 'broken' (read) failed while running query" in output
    assert "query:\nselect * from broken_table" in output


@pytest.mark.parametrize(
    ("tasks", "expected_exception"),
    [
        ([], ValueError),
        ({}, TypeError),
        ([{"name": "", "type": "read"}], ValueError),
        ([{"type": "read"}, "read"], TypeError),
        ([{"db_key": "gp"}], ValueError),
        ([{"type": "unknown"}], ValueError),
        ([{"type": ["read"]}], ValueError),
    ],
)
def test_async_sql_validates_task_input(
    tasks: Any,
    expected_exception: type[Exception],
) -> None:
    with pytest.raises(expected_exception):
        async_module.async_sql(tasks)


@pytest.mark.parametrize("concurrency", [0, -1, True, 1.5])
def test_async_sql_validates_concurrency(concurrency: Any) -> None:
    tasks = [
        {
            "type": "read",
            "db_key": "gp",
            "query": "select 1",
        }
    ]

    with pytest.raises(ValueError, match="concurrency"):
        async_module.async_sql(tasks, concurrency=concurrency)


@pytest.mark.parametrize("soft_concurrency_cap", [0, -1, True, 1.5])
def test_async_sql_validates_soft_concurrency_cap(
    soft_concurrency_cap: Any,
) -> None:
    tasks = [
        {
            "type": "read",
            "db_key": "gp",
            "query": "select 1",
        }
    ]

    with pytest.raises(ValueError, match="soft_concurrency_cap"):
        async_module.async_sql(
            tasks,
            soft_concurrency_cap=soft_concurrency_cap,
        )


@pytest.mark.parametrize("hard_concurrency_cap", [0, -1, True, 1.5])
def test_async_sql_validates_hard_concurrency_cap(
    hard_concurrency_cap: Any,
) -> None:
    tasks = [
        {
            "type": "read",
            "db_key": "gp",
            "query": "select 1",
        }
    ]

    with pytest.raises(ValueError, match="hard_concurrency_cap"):
        async_module.async_sql(
            tasks,
            hard_concurrency_cap=hard_concurrency_cap,
        )


@pytest.mark.parametrize("progress", [None, 0, 1, "yes"])
def test_async_sql_validates_progress(progress: Any) -> None:
    tasks = [
        {
            "type": "read",
            "db_key": "gp",
            "query": "select 1",
        }
    ]

    with pytest.raises(ValueError, match="progress"):
        async_module.async_sql(tasks, progress=progress)


@pytest.mark.parametrize(
    ("spec", "expected_exception"),
    [
        ({"type": "custom_sql_pipeline"}, ValueError),
        ({"type": "custom_sql_pipeline", "steps": []}, ValueError),
        ({"type": "custom_sql_pipeline", "steps": "not steps"}, TypeError),
        ({"type": "custom_sql_pipeline", "steps": b"not steps"}, TypeError),
        ({"type": "custom_sql_pipeline", "steps": object()}, TypeError),
        (
            {"type": "custom_sql_pipeline", "steps": [lambda context: None, 1]},
            TypeError,
        ),
    ],
)
def test_async_sql_validates_pipeline_steps(
    spec: dict[str, Any],
    expected_exception: type[Exception],
) -> None:
    with pytest.raises(expected_exception, match="steps|step"):
        async_module.async_sql([{"name": "pipeline", **spec}])


def test_async_sql_validates_pipeline_extra_fields() -> None:
    with pytest.raises(ValueError, match="unsupported custom_sql_pipeline field"):
        async_module.async_sql(
            [
                {
                    "name": "pipeline",
                    "type": "custom_sql_pipeline",
                    "steps": [lambda context: None],
                    "db_key": "gp",
                }
            ]
        )

    with pytest.raises(ValueError, match="unsupported custom_sql_pipeline field"):
        async_module.async_sql(
            [
                {
                    "name": "pipeline",
                    "type": "custom_sql_pipeline",
                    "steps": [lambda context: None],
                    "start_comment": "-- unsupported",
                }
            ]
        )
