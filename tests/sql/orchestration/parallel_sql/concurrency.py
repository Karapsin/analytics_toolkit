from __future__ import annotations

from tests._support.concurrency import ConcurrencyProbe
from tests.sql._support.parallel_sql import (
    Any,
    inspect,
    named_tasks,
    parallel_module,
    pytest,
    sql_module,
)
from tests.sql._support.tasks import observe_task_cap, patch_task_operations


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
    probe = ConcurrencyProbe(2, held=True)

    def fake_read_sql(**kwargs: Any) -> str:
        with probe.worker():
            return kwargs["query"]

    patch_task_operations(monkeypatch, mode="parallel", read=fake_read_sql)

    observation = observe_task_cap(monkeypatch, probe, mode="parallel", calls=6)
    result = observation.run(
        lambda: sql_module.parallel_sql(
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
    )

    assert list(result) == [f"read_{index}" for index in range(6)]
    probe.assert_complete(calls=6)


def test_parallel_sql_defaults_hard_concurrency_cap_to_five() -> None:
    assert (
        inspect.signature(sql_module.parallel_sql).parameters["hard_concurrency_cap"].default == 5
    )
