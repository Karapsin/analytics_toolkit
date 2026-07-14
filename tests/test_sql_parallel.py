from __future__ import annotations

import importlib
import threading
import time
from typing import Any

import pandas as pd
import pytest

from analytics_toolkit.general import time_print

parallel_module = importlib.import_module("analytics_toolkit.sql.orchestration.parallel_sql")
tasks_module = importlib.import_module("analytics_toolkit.sql.orchestration.tasks")
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


def test_parallel_sql_task_context_task_id_is_included_in_task_output(
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

    monkeypatch.setattr(parallel_module, "read_sql", fake_read_sql)

    result = parallel_module.parallel_sql(
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


def test_parallel_sql_task_context_task_id_is_included_for_failed_tasks(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fake_read_sql(
        *,
        db_key: str,
        query: str,
        print_queries: bool = False,
    ) -> pd.DataFrame:
        time_print("task output before failure")
        raise RuntimeError("boom")

    monkeypatch.setattr(parallel_module, "read_sql", fake_read_sql)

    with pytest.raises(RuntimeError, match="boom"):
        parallel_module.parallel_sql(
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

    assert "[task_id=copy_table]" in capsys.readouterr().out


def test_parallel_sql_is_exported() -> None:
    assert sql_module.parallel_sql is parallel_module.parallel_sql


def test_parallel_sql_dispatches_supported_task_types_and_preserves_order(
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
        progress: bool = False,
    ) -> None:
        calls.append(
            (
                "execute",
                {
                    "db_key": db_key,
                    "query": query,
                    "gp_break_query": gp_break_query,
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
                    "progress": progress,
                },
            )
        )
        return transfer_result

    monkeypatch.setattr(parallel_module, "read_sql", fake_read_sql)
    monkeypatch.setattr(parallel_module, "execute_sql", fake_execute_sql)
    monkeypatch.setattr(parallel_module, "execute_read", fake_execute_read)
    monkeypatch.setattr(parallel_module, "load_df", fake_load_df)
    monkeypatch.setattr(parallel_module, "transfer_table", fake_transfer_table)

    result = parallel_module.parallel_sql(
        named_tasks(
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
                },
            }
        ),
        concurrency=3,
    )

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
def test_parallel_sql_rejects_removed_connection_task_arguments(
    task_type: str,
    forbidden_field: str,
) -> None:
    task = sql_task_spec(task_type)
    task[forbidden_field] = "gp"

    with pytest.raises(
        ValueError,
        match=rf"unsupported SQL task argument.*{forbidden_field}",
    ):
        parallel_module.parallel_sql([task], concurrency=1, progress=False)


def test_parallel_sql_start_comment_prefixes_sql_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_read_sql(**kwargs: Any) -> str:
        return kwargs["query"]

    def fake_execute_sql(**kwargs: Any) -> None:
        return None

    def fake_execute_read(**kwargs: Any) -> str:
        return kwargs["query"]

    def fake_transfer_table(**kwargs: Any) -> str:
        return kwargs["from_sql"]

    monkeypatch.setattr(parallel_module, "read_sql", fake_read_sql)
    monkeypatch.setattr(parallel_module, "execute_sql", fake_execute_sql)
    monkeypatch.setattr(parallel_module, "execute_read", fake_execute_read)
    monkeypatch.setattr(parallel_module, "transfer_table", fake_transfer_table)

    result = parallel_module.parallel_sql(
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
        start_comment="/* parallel batch */  \n",
        progress=False,
    )

    assert result == {
        "read_users": "/* parallel batch */\nselect * from users",
        "refresh_table": "success",
        "prepare_and_read": (
            "/* parallel batch */\ncreate table tmp as select 1; select * from tmp"
        ),
        "copy_table": "/* parallel batch */\nselect * from source",
    }


def test_parallel_sql_uses_generated_names_for_unnamed_task_sequence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_execute_sql(**kwargs: Any) -> str:
        return kwargs["query"]

    monkeypatch.setattr(parallel_module, "execute_sql", fake_execute_sql)

    result = parallel_module.parallel_sql(
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
        progress=False,
    )

    assert result == {
        "task_0": "insert into target select 1",
        "task_1": "insert into target select 2",
    }


def test_parallel_sql_concurrency_limits_active_top_level_work(
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

    monkeypatch.setattr(parallel_module, "read_sql", fake_read_sql)

    result = parallel_module.parallel_sql(
        named_tasks(
            {
                f"read_{index}": {
                    "type": "read",
                    "db_key": "gp",
                    "query": f"select {index}",
                }
                for index in range(6)
            }
        ),
        concurrency=2,
    )

    assert list(result) == [f"read_{index}" for index in range(6)]
    assert max_active_tasks == 2


def test_parallel_sql_soft_cap_limits_actual_worker_execution(
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

    monkeypatch.setattr(parallel_module, "read_sql", fake_read_sql)

    result = parallel_module.parallel_sql(
        named_tasks(
            {
                f"read_{index}": {
                    "type": "read",
                    "db_key": "gp",
                    "query": f"select {index}",
                }
                for index in range(6)
            }
        ),
        concurrency=6,
        soft_concurrency_cap=2,
    )

    assert list(result) == [f"read_{index}" for index in range(6)]
    assert max_active_workers == 2


def test_parallel_sql_soft_cap_limits_pipeline_step_execution() -> None:
    lock = threading.Lock()
    active_steps = 0
    max_active_steps = 0

    def pipeline_step(context: Any) -> str:
        nonlocal active_steps, max_active_steps
        with lock:
            active_steps += 1
            max_active_steps = max(max_active_steps, active_steps)
        time.sleep(0.1)
        with lock:
            active_steps -= 1
        return context.task_name

    result = parallel_module.parallel_sql(
        named_tasks(
            {
                f"pipeline_{index}": {
                    "type": "custom_sql_pipeline",
                    "steps": [pipeline_step],
                }
                for index in range(6)
            }
        ),
        concurrency=6,
        soft_concurrency_cap=2,
        progress=False,
    )

    assert list(result) == [f"pipeline_{index}" for index in range(6)]
    assert max_active_steps == 2


def test_parallel_sql_nested_pipeline_respects_soft_cap(
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

    def nested_batch(context: Any) -> dict[str, Any]:
        return parallel_module.parallel_sql(
            named_tasks(
                {
                    f"read_{index}": {
                        "type": "read",
                        "db_key": "gp",
                        "query": f"{context.task_name}:{index}",
                    }
                    for index in range(6)
                }
            ),
            concurrency=6,
            progress=False,
        )

    monkeypatch.setattr(parallel_module, "read_sql", fake_read_sql)

    result = parallel_module.parallel_sql(
        [
            {
                "name": "pipeline",
                "type": "custom_sql_pipeline",
                "steps": [nested_batch],
            }
        ],
        concurrency=1,
        soft_concurrency_cap=2,
        progress=False,
    )

    assert list(result["pipeline"]) == [f"read_{index}" for index in range(6)]
    assert max_active_workers == 2


def test_parallel_sql_hard_cap_rejects_excessive_nested_concurrency() -> None:
    def nested_batch(context: Any) -> dict[str, Any]:
        return parallel_module.parallel_sql(
            [
                {
                    "type": "read",
                    "db_key": "gp",
                    "query": "select 1",
                }
            ],
            concurrency=2,
            progress=False,
        )

    with pytest.raises(
        ValueError,
        match=("effective concurrency exceeds hard_concurrency_cap.*soft_concurrency_cap"),
    ):
        parallel_module.parallel_sql(
            [
                {
                    "name": "pipeline",
                    "type": "custom_sql_pipeline",
                    "steps": [nested_batch],
                }
            ],
            concurrency=6,
            soft_concurrency_cap=20,
            hard_concurrency_cap=10,
            progress=False,
        )


def test_parallel_sql_pipeline_runs_steps_sequentially_and_returns_last_result() -> None:
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

    def second_step(context: Any) -> str:
        observations.append(
            (
                context.task_name,
                context.step_index,
                list(context.results),
                context.last_result,
            )
        )
        return f"{context.last_result}:second"

    result = parallel_module.parallel_sql(
        [
            {
                "name": "pipeline",
                "type": "custom_sql_pipeline",
                "steps": [first_step, second_step],
            }
        ],
        progress=False,
    )

    assert result["pipeline"] == "first:second"
    assert observations == [
        ("pipeline", 0, [], None),
        ("pipeline", 1, ["first"], "first"),
    ]


@pytest.mark.parametrize("step_factory", ["async_function", "coroutine_result"])
def test_parallel_sql_rejects_async_pipeline_steps(step_factory: str) -> None:
    async def async_step(context: Any) -> str:
        return context.task_name

    def coroutine_step(context: Any) -> Any:
        return async_step(context)

    step = async_step if step_factory == "async_function" else coroutine_step

    with pytest.raises(TypeError, match="use async_sql for async pipeline steps"):
        parallel_module.parallel_sql(
            [
                {
                    "name": "pipeline",
                    "type": "custom_sql_pipeline",
                    "steps": [step],
                }
            ],
            progress=False,
        )


def test_parallel_sql_fail_fast_raises_first_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = RuntimeError("read failed")

    def fake_read_sql(**kwargs: Any) -> str:
        raise error

    monkeypatch.setattr(parallel_module, "read_sql", fake_read_sql)

    with pytest.raises(RuntimeError) as exc_info:
        parallel_module.parallel_sql(
            named_tasks(
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
            ),
            concurrency=1,
            fail_fast=True,
            progress=False,
        )

    assert exc_info.value is error


def test_parallel_sql_fail_fast_true_prints_failed_transfer_sql(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    error = RuntimeError("transfer failed")

    def fake_transfer_table(**kwargs: Any) -> str:
        raise error

    monkeypatch.setattr(parallel_module, "transfer_table", fake_transfer_table)

    with pytest.raises(RuntimeError) as exc_info:
        parallel_module.parallel_sql(
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


def test_parallel_sql_fail_fast_false_returns_exceptions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = RuntimeError("read failed")

    def fake_read_sql(**kwargs: Any) -> str:
        if kwargs["query"] == "select broken":
            raise error
        return kwargs["query"]

    def fake_execute_sql(**kwargs: Any) -> None:
        return None

    monkeypatch.setattr(parallel_module, "read_sql", fake_read_sql)
    monkeypatch.setattr(parallel_module, "execute_sql", fake_execute_sql)

    result = parallel_module.parallel_sql(
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
        progress=False,
    )

    assert result["ok"] == "select ok"
    assert result["broken"] == str(error)
    assert result["write_ok"] == "success"


def test_parallel_sql_fail_fast_false_prints_failed_task_query(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    error = RuntimeError("read failed")

    def fake_read_sql(**kwargs: Any) -> str:
        raise error

    monkeypatch.setattr(parallel_module, "read_sql", fake_read_sql)

    result = parallel_module.parallel_sql(
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


def test_parallel_sql_updates_progress_bar(monkeypatch: pytest.MonkeyPatch) -> None:
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

    monkeypatch.setattr(parallel_module, "tqdm", FakeTqdm)
    monkeypatch.setattr(parallel_module, "execute_sql", fake_execute_sql)

    result = parallel_module.parallel_sql(
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
        "desc": "parallel_sql tasks",
        "unit": "task",
        "disable": False,
    }
    assert progress_bar.updates == [1, 1]
    assert progress_bar.closed


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
def test_parallel_sql_validates_task_input(
    tasks: Any,
    expected_exception: type[Exception],
) -> None:
    with pytest.raises(expected_exception):
        parallel_module.parallel_sql(tasks)


def test_parallel_shutdown_falls_back_for_legacy_executor() -> None:
    unsupported_error = TypeError("unsupported")

    class LegacyExecutor:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def shutdown(self, **kwargs: object) -> None:
            self.calls.append(kwargs)
            if "cancel_futures" in kwargs:
                raise unsupported_error

    executor = LegacyExecutor()
    parallel_module._shutdown_executor(
        executor,
        wait=True,
        cancel_futures=True,
    )
    assert executor.calls == [
        {"wait": True, "cancel_futures": True},
        {"wait": True},
    ]


def test_parallel_nested_concurrency_state_can_raise_caps_and_add_semaphore() -> None:
    active = parallel_module._ParallelConcurrencyState(
        effective_concurrency=1,
        hard_cap=2,
        soft_cap=2,
        semaphores=(parallel_module.Semaphore(2),),
    )
    token = parallel_module._PARALLEL_CONCURRENCY_STATE.set(active)
    try:
        state = parallel_module._build_parallel_concurrency_state(
            concurrency=2,
            soft_concurrency_cap=1,
            hard_concurrency_cap=3,
        )
    finally:
        parallel_module._PARALLEL_CONCURRENCY_STATE.reset(token)
    assert state.hard_cap == 3
    assert state.soft_cap == 1
    assert len(state.semaphores) == 2


def test_parallel_pipeline_rejects_non_closable_awaitable() -> None:
    class Awaitable:
        def __await__(self):
            if False:
                yield None

    with pytest.raises(TypeError, match="coroutine custom_sql_pipeline"):
        parallel_module._run_parallel_pipeline(
            "pipeline",
            [lambda _context: Awaitable()],
            (),
        )


def test_task_helpers_handle_locked_exception_blank_query_and_unknown_type() -> None:
    class LockedError(Exception):
        def __setattr__(self, _name: str, _value: object) -> None:
            message = "locked"
            raise RuntimeError(message)

    tasks_module._annotate_task_exception(
        LockedError("failure"),
        "task",
        "read",
        {"query": "select 1"},
    )
    assert tasks_module._task_sql_field_and_query("read", {"query": " "}) == (
        "query",
        None,
    )
    assert tasks_module._apply_start_comment(
        "read",
        {"query": 1},
        "-- start",
    ) == {"query": 1}
    with pytest.raises(ValueError, match="Unsupported task type"):
        tasks_module._run_sync_task("unknown", {}, task_runners={})


@pytest.mark.parametrize(
    "tasks",
    [
        [
            {"name": "duplicate", "type": "read", "db_key": "gp", "query": "select 1"},
            {"name": "duplicate", "type": "read", "db_key": "gp", "query": "select 2"},
        ],
        [
            {"name": "task_1", "type": "read", "db_key": "gp", "query": "select 1"},
            {"type": "read", "db_key": "gp", "query": "select 2"},
        ],
    ],
)
def test_parallel_sql_rejects_duplicate_task_names(
    tasks: list[dict[str, Any]],
) -> None:
    with pytest.raises(ValueError, match="Duplicate SQL task name"):
        parallel_module.parallel_sql(tasks)


@pytest.mark.parametrize("concurrency", [0, -1, True, 1.5])
def test_parallel_sql_validates_concurrency(concurrency: Any) -> None:
    with pytest.raises(ValueError, match="concurrency"):
        parallel_module.parallel_sql(
            [{"type": "read", "db_key": "gp", "query": "select 1"}],
            concurrency=concurrency,
        )


@pytest.mark.parametrize("soft_concurrency_cap", [0, -1, True, 1.5])
def test_parallel_sql_validates_soft_concurrency_cap(
    soft_concurrency_cap: Any,
) -> None:
    with pytest.raises(ValueError, match="soft_concurrency_cap"):
        parallel_module.parallel_sql(
            [{"type": "read", "db_key": "gp", "query": "select 1"}],
            soft_concurrency_cap=soft_concurrency_cap,
        )


@pytest.mark.parametrize("hard_concurrency_cap", [0, -1, True, 1.5])
def test_parallel_sql_validates_hard_concurrency_cap(
    hard_concurrency_cap: Any,
) -> None:
    with pytest.raises(ValueError, match="hard_concurrency_cap"):
        parallel_module.parallel_sql(
            [{"type": "read", "db_key": "gp", "query": "select 1"}],
            hard_concurrency_cap=hard_concurrency_cap,
        )


@pytest.mark.parametrize("progress", [None, 0, 1, "yes"])
def test_parallel_sql_validates_progress(progress: Any) -> None:
    with pytest.raises(ValueError, match="progress"):
        parallel_module.parallel_sql(
            [{"type": "read", "db_key": "gp", "query": "select 1"}],
            progress=progress,
        )
