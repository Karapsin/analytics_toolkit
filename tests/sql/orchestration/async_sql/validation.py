from __future__ import annotations

from tests.sql._support.async_sql import (
    Any,
    async_module,
    pd,
    pytest,
    sql_task_spec,
)


def test_async_sql_default_hard_cap_rejects_six_workers() -> None:
    with pytest.raises(ValueError, match=r"effective concurrency.*\(6 > 5\)"):
        async_module.async_sql(
            [{"type": "read", "db_key": "gp", "query": "select 1"}],
            concurrency=6,
        )


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
        match=("effective concurrency exceeds hard_concurrency_cap.*soft_concurrency_cap"),
    ):
        async_module.async_sql(tasks, concurrency=11)


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
def test_async_sql_rejects_duplicate_task_names(
    tasks: list[dict[str, Any]],
) -> None:
    with pytest.raises(ValueError, match="Duplicate SQL task name"):
        async_module.async_sql(tasks)


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
