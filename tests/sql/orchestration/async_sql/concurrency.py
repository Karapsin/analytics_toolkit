from __future__ import annotations

from tests.sql._support.async_sql import (
    Any,
    async_module,
    asyncio,
    inspect,
    named_tasks,
    pytest,
    sql_module,
    threading,
    time,
)


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


def test_async_sql_defaults_hard_concurrency_cap_to_five() -> None:
    assert inspect.signature(sql_module.async_sql).parameters["hard_concurrency_cap"].default == 5
