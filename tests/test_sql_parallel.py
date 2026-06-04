from __future__ import annotations

import importlib
import threading
import time
from typing import Any

import pandas as pd
import pytest

async_module = importlib.import_module("analytics_toolkit.sql.orchestration.async_api")
sql_module = importlib.import_module("analytics_toolkit.sql")


def named_tasks(tasks: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"name": name, **spec} for name, spec in tasks.items()]


def test_parallel_sql_is_exported() -> None:
    assert sql_module.parallel_sql is async_module.parallel_sql


def test_parallel_sql_dispatches_supported_task_types_and_preserves_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    read_result = pd.DataFrame({"value": [1]})
    execute_read_result = pd.DataFrame({"value": [2]})
    load_result = 3
    transfer_result = 4
    df = pd.DataFrame({"id": [1]})

    def record(task_type: str, result: Any):
        def fake_operation(**kwargs: Any) -> Any:
            calls.append((task_type, kwargs))
            return result

        return fake_operation

    monkeypatch.setattr(async_module, "read_sql", record("read", read_result))
    monkeypatch.setattr(async_module, "execute_sql", record("execute", None))
    monkeypatch.setattr(
        async_module,
        "execute_read",
        record("execute_read", execute_read_result),
    )
    monkeypatch.setattr(async_module, "load_df", record("load_df", load_result))
    monkeypatch.setattr(
        async_module,
        "transfer_table",
        record("transfer", transfer_result),
    )

    result = async_module.parallel_sql(
        named_tasks(
            {
                "read_users": {
                    "type": "read",
                    "connection_type": "gp",
                    "query": "select * from users",
                    "print_queries": False,
                },
                "refresh_table": {
                    "type": "execute",
                    "connection_type": "gp",
                    "query": "truncate table sandbox.target",
                    "gp_break_query": True,
                },
                "prepare_and_read": {
                    "type": "execute_read",
                    "connection_type": "trino",
                    "query": "create table tmp as select 1; select * from tmp",
                },
                "load_batch": {
                    "type": "load_df",
                    "connection_type": "ch",
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
        "connection_type": "gp",
        "query": "select * from users",
        "print_queries": False,
    }
    assert calls_by_type["execute"] == {
        "connection_type": "gp",
        "query": "truncate table sandbox.target",
        "gp_break_query": True,
        "progress": False,
    }
    assert calls_by_type["execute_read"] == {
        "connection_type": "trino",
        "query": "create table tmp as select 1; select * from tmp",
        "progress": False,
    }
    load_kwargs = calls_by_type["load_df"]
    assert load_kwargs["df"] is df
    assert {key: value for key, value in load_kwargs.items() if key != "df"} == {
        "connection_type": "ch",
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

    monkeypatch.setattr(async_module, "read_sql", fake_read_sql)
    monkeypatch.setattr(async_module, "execute_sql", fake_execute_sql)
    monkeypatch.setattr(async_module, "execute_read", fake_execute_read)
    monkeypatch.setattr(async_module, "transfer_table", fake_transfer_table)

    result = async_module.parallel_sql(
        named_tasks(
            {
                "read_users": {
                    "type": "read",
                    "connection_type": "gp",
                    "query": "select * from users",
                },
                "refresh_table": {
                    "type": "execute",
                    "connection_type": "gp",
                    "query": "truncate table sandbox.target",
                },
                "prepare_and_read": {
                    "type": "execute_read",
                    "connection_type": "trino",
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

    monkeypatch.setattr(async_module, "execute_sql", fake_execute_sql)

    result = async_module.parallel_sql(
        [
            {
                "type": "execute",
                "connection_type": "gp",
                "query": "insert into target select 1",
            },
            {
                "type": "execute",
                "connection_type": "gp",
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

    monkeypatch.setattr(async_module, "read_sql", fake_read_sql)

    result = async_module.parallel_sql(
        named_tasks(
            {
                f"read_{index}": {
                    "type": "read",
                    "connection_type": "gp",
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

    monkeypatch.setattr(async_module, "read_sql", fake_read_sql)

    result = async_module.parallel_sql(
        named_tasks(
            {
                f"read_{index}": {
                    "type": "read",
                    "connection_type": "gp",
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

    result = async_module.parallel_sql(
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
        return async_module.parallel_sql(
            named_tasks(
                {
                    f"read_{index}": {
                        "type": "read",
                        "connection_type": "gp",
                        "query": f"{context.task_name}:{index}",
                    }
                    for index in range(6)
                }
            ),
            concurrency=6,
            progress=False,
        )

    monkeypatch.setattr(async_module, "read_sql", fake_read_sql)

    result = async_module.parallel_sql(
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
        return async_module.parallel_sql(
            [
                {
                    "type": "read",
                    "connection_type": "gp",
                    "query": "select 1",
                }
            ],
            concurrency=2,
            progress=False,
        )

    with pytest.raises(
        ValueError,
        match=(
            "effective concurrency exceeds hard_concurrency_cap.*"
            "soft_concurrency_cap"
        ),
    ):
        async_module.parallel_sql(
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

    result = async_module.parallel_sql(
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
        async_module.parallel_sql(
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

    monkeypatch.setattr(async_module, "read_sql", fake_read_sql)

    with pytest.raises(RuntimeError) as exc_info:
        async_module.parallel_sql(
            named_tasks(
                {
                    "broken": {
                        "type": "read",
                        "connection_type": "gp",
                        "query": "select broken",
                    },
                    "also_broken": {
                        "type": "read",
                        "connection_type": "gp",
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

    monkeypatch.setattr(async_module, "transfer_table", fake_transfer_table)

    with pytest.raises(RuntimeError) as exc_info:
        async_module.parallel_sql(
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

    monkeypatch.setattr(async_module, "read_sql", fake_read_sql)
    monkeypatch.setattr(async_module, "execute_sql", fake_execute_sql)

    result = async_module.parallel_sql(
        named_tasks(
            {
                "ok": {
                    "type": "read",
                    "connection_type": "gp",
                    "query": "select ok",
                },
                "broken": {
                    "type": "read",
                    "connection_type": "gp",
                    "query": "select broken",
                },
                "write_ok": {
                    "type": "execute",
                    "connection_type": "gp",
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

    monkeypatch.setattr(async_module, "read_sql", fake_read_sql)

    result = async_module.parallel_sql(
        [
            {
                "name": "broken",
                "type": "read",
                "connection_type": "gp",
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

    monkeypatch.setattr(async_module, "tqdm", FakeTqdm)
    monkeypatch.setattr(async_module, "execute_sql", fake_execute_sql)

    result = async_module.parallel_sql(
        [
            {
                "type": "execute",
                "connection_type": "gp",
                "query": "insert into target select 1",
            },
            {
                "type": "execute",
                "connection_type": "gp",
                "query": "insert into target select 2",
            },
        ],
        concurrency=1,
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
        ([{"connection_type": "gp"}], ValueError),
        ([{"type": "unknown"}], ValueError),
        ([{"type": ["read"]}], ValueError),
    ],
)
def test_parallel_sql_validates_task_input(
    tasks: Any,
    expected_exception: type[Exception],
) -> None:
    with pytest.raises(expected_exception):
        async_module.parallel_sql(tasks)


@pytest.mark.parametrize("concurrency", [0, -1, True, 1.5])
def test_parallel_sql_validates_concurrency(concurrency: Any) -> None:
    with pytest.raises(ValueError, match="concurrency"):
        async_module.parallel_sql(
            [{"type": "read", "connection_type": "gp", "query": "select 1"}],
            concurrency=concurrency,
        )


@pytest.mark.parametrize("soft_concurrency_cap", [0, -1, True, 1.5])
def test_parallel_sql_validates_soft_concurrency_cap(
    soft_concurrency_cap: Any,
) -> None:
    with pytest.raises(ValueError, match="soft_concurrency_cap"):
        async_module.parallel_sql(
            [{"type": "read", "connection_type": "gp", "query": "select 1"}],
            soft_concurrency_cap=soft_concurrency_cap,
        )


@pytest.mark.parametrize("hard_concurrency_cap", [0, -1, True, 1.5])
def test_parallel_sql_validates_hard_concurrency_cap(
    hard_concurrency_cap: Any,
) -> None:
    with pytest.raises(ValueError, match="hard_concurrency_cap"):
        async_module.parallel_sql(
            [{"type": "read", "connection_type": "gp", "query": "select 1"}],
            hard_concurrency_cap=hard_concurrency_cap,
        )


@pytest.mark.parametrize("progress", [None, 0, 1, "yes"])
def test_parallel_sql_validates_progress(progress: Any) -> None:
    with pytest.raises(ValueError, match="progress"):
        async_module.parallel_sql(
            [{"type": "read", "connection_type": "gp", "query": "select 1"}],
            progress=progress,
        )
