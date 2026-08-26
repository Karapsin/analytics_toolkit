from __future__ import annotations

from tests.sql._support.parallel_sql import (
    Any,
    parallel_module,
    pytest,
    tasks_module,
)


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
