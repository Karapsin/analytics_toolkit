from __future__ import annotations

from tests.sql._support.parallel_sql import (
    Any,
    inspect,
    named_tasks,
    parallel_module,
    pytest,
    sql_module,
    threading,
    time,
)


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


def test_parallel_sql_defaults_hard_concurrency_cap_to_five() -> None:
    assert (
        inspect.signature(sql_module.parallel_sql).parameters["hard_concurrency_cap"].default == 5
    )
