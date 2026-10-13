"""Fresh task specifications shared by async and threaded orchestration tests."""

from __future__ import annotations

import asyncio
import importlib
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pandas as pd

from tests._support.concurrency import ConcurrencyProbe, SchedulingObservation

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Literal

    import pytest


def patch_task_operations(
    monkeypatch: pytest.MonkeyPatch,
    *,
    mode: Literal["async", "parallel"],
    **operations: Callable[..., Any],
) -> None:
    """Keep orchestration's operation patch locations in one compatibility seam."""
    module_name = {"async": "async_sql", "parallel": "parallel_sql"}[mode]
    module = importlib.import_module(f"analytics_toolkit.sql.orchestration.{module_name}")
    targets = {
        "read": "read_sql",
        "execute": "execute_sql",
        "execute_read": "execute_read",
        "load_df": "load_df",
        "transfer": "transfer_table",
    }
    unknown = set(operations) - set(targets)
    if unknown:
        message = f"Unsupported fake task operations: {sorted(unknown)}"
        raise ValueError(message)
    for operation, runner in operations.items():
        monkeypatch.setattr(module, targets[operation], runner)


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
    message = f"Unsupported task type for test: {task_type}"
    raise ValueError(message)


def observe_task_cap(
    monkeypatch: pytest.MonkeyPatch,
    probe: ConcurrencyProbe,
    *,
    mode: Literal["async", "parallel"],
    calls: int,
    nested: bool = False,
) -> SchedulingObservation:
    """Observe real SQL scheduling; nested observations count only leaf tasks."""
    module_name = {"async": "async_sql", "parallel": "parallel_sql"}[mode]
    module = importlib.import_module(f"analytics_toolkit.sql.orchestration.{module_name}")

    class TaskObservation(SchedulingObservation):
        def run(self, operation: Callable[[], Any]) -> Any:
            with monkeypatch.context() as patch:
                configure(patch)
                return super().run(operation)

    observation = TaskObservation(probe, calls=calls)

    def configure(patch: pytest.MonkeyPatch) -> None:
        if mode == "parallel":

            def include(args: tuple[Any, ...]) -> bool:
                # context.run(_run_parallel_indexed, index, name, task_type, ...).
                return not (nested and len(args) > 3 and args[3] == "custom_sql_pipeline")

            patch.setattr(module, "Semaphore", observation.semaphore_factory(module.Semaphore))
            patch.setattr(
                module,
                "ThreadPoolExecutor",
                observation.executor_factory(module.ThreadPoolExecutor, include=include),
            )
        else:
            original = asyncio.Semaphore

            class ObservedAsyncSemaphore(original):
                async def acquire(self) -> bool:
                    candidate = asyncio.current_task()
                    assert candidate is not None
                    observation.register(candidate)
                    # On the owning loop no task can run between locked() and acquire().
                    # Use the real acquire to preserve waiter fairness and cancellation.
                    if self.locked():
                        observation.blocked(candidate)
                    return await super().acquire()

            def current_task() -> object:
                candidate = asyncio.current_task()
                assert candidate is not None
                return candidate

            patch.setattr(asyncio, "Semaphore", ObservedAsyncSemaphore)
            patch.setattr(
                module,
                "ThreadPoolExecutor",
                observation.executor_factory(module.ThreadPoolExecutor, candidate_key=current_task),
            )

    return observation


def bypass_task_soft_cap(
    monkeypatch: pytest.MonkeyPatch, *, mode: Literal["async", "parallel"]
) -> None:
    """Intentional test fault: bypass both soft semaphores and executor throttling."""
    module_name = {"async": "async_sql", "parallel": "parallel_sql"}[mode]
    module = importlib.import_module(f"analytics_toolkit.sql.orchestration.{module_name}")
    name = "_build_concurrency_state" if mode == "async" else "_build_parallel_concurrency_state"
    original = getattr(module, name)

    def uncapped(**kwargs: Any) -> Any:
        state = original(**kwargs)
        return replace(state, soft_cap=kwargs["concurrency"], semaphores=())

    monkeypatch.setattr(module, name, uncapped)
