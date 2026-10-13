from __future__ import annotations

from tests._support.concurrency import ConcurrencyProbe
from tests.sql._support.async_sql import (
    Any,
    async_module,
    asyncio,
    inspect,
    named_tasks,
    pytest,
    sql_module,
)
from tests.sql._support.tasks import observe_task_cap, patch_task_operations


def test_async_nested_concurrency_state_can_raise_caps_and_add_semaphore() -> None:
    async def build_state() -> Any:
        active = async_module._ConcurrencyState(
            effective_concurrency=1,
            hard_cap=2,
            soft_cap=2,
            semaphores=(asyncio.Semaphore(2),),
        )
        token = async_module._CONCURRENCY_STATE.set(active)
        try:
            return async_module._build_concurrency_state(
                concurrency=2,
                soft_concurrency_cap=1,
                hard_concurrency_cap=3,
            )
        finally:
            async_module._CONCURRENCY_STATE.reset(token)

    state = asyncio.run(build_state())
    assert state.hard_cap == 3
    assert state.soft_cap == 1
    assert len(state.semaphores) == 2


def test_async_sql_concurrency_limits_active_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = ConcurrencyProbe(2, held=True)

    def fake_read_sql(**kwargs: Any) -> str:
        with probe.worker():
            return kwargs["query"]

    patch_task_operations(monkeypatch, mode="async", read=fake_read_sql)

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

    observation = observe_task_cap(monkeypatch, probe, mode="async", calls=6)
    result = observation.run(lambda: sql_module.async_sql(tasks, concurrency=2))

    assert list(result) == [f"read_{index}" for index in range(6)]
    probe.assert_complete(calls=6)


def test_async_sql_defaults_hard_concurrency_cap_to_five() -> None:
    assert inspect.signature(sql_module.async_sql).parameters["hard_concurrency_cap"].default == 5
