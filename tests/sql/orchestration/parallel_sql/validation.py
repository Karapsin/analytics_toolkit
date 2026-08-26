from __future__ import annotations

from tests.sql._support.parallel_sql import (
    Any,
    parallel_module,
    pytest,
    sql_task_spec,
)


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


def test_parallel_sql_default_hard_cap_rejects_six_workers() -> None:
    with pytest.raises(ValueError, match=r"effective concurrency.*\(6 > 5\)"):
        parallel_module.parallel_sql(
            [{"type": "read", "db_key": "gp", "query": "select 1"}],
            concurrency=6,
        )


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


@pytest.mark.parametrize("concurrency", [0, -1, True, 1.5])
def test_parallel_sql_validates_concurrency(concurrency: Any) -> None:
    with pytest.raises(ValueError, match="concurrency"):
        parallel_module.parallel_sql(
            [{"type": "read", "db_key": "gp", "query": "select 1"}],
            concurrency=concurrency,
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


@pytest.mark.parametrize("soft_concurrency_cap", [0, -1, True, 1.5])
def test_parallel_sql_validates_soft_concurrency_cap(
    soft_concurrency_cap: Any,
) -> None:
    with pytest.raises(ValueError, match="soft_concurrency_cap"):
        parallel_module.parallel_sql(
            [{"type": "read", "db_key": "gp", "query": "select 1"}],
            soft_concurrency_cap=soft_concurrency_cap,
        )


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
