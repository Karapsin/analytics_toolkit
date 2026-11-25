"""Public nested batches share limits across both execution entrypoints."""

from __future__ import annotations

import importlib
from typing import Any

import pytest
from analytics_toolkit import sql

from tests._support.concurrency import ConcurrencyProbe, SchedulingObservation
from tests.sql._support.tasks import patch_task_operations


@pytest.mark.parametrize(
    ("outer_mode", "child_mode"), [("async", "parallel"), ("parallel", "async")]
)
def test_mixed_nested_batches_keep_the_ancestor_worker_cap(
    monkeypatch: pytest.MonkeyPatch, outer_mode: str, child_mode: str
) -> None:
    probe = ConcurrencyProbe(1, held=True)
    observation = SchedulingObservation(probe, calls=3)
    modules = {
        mode: importlib.import_module(f"analytics_toolkit.sql.orchestration.{mode}_sql")
        for mode in ("async", "parallel")
    }

    def read(**kwargs: Any) -> str:
        with probe.worker():
            return kwargs["query"]

    patch_task_operations(monkeypatch, mode=child_mode, read=read)
    for module in modules.values():
        monkeypatch.setattr(module, "Semaphore", observation.semaphore_factory(module.Semaphore))
    child_module = modules[child_mode]
    monkeypatch.setattr(
        child_module,
        "ThreadPoolExecutor",
        observation.executor_factory(child_module.ThreadPoolExecutor),
    )
    leaves = [
        {"name": str(index), "type": "read", "db_key": "gp", "query": str(index)}
        for index in range(3)
    ]

    def child(_context: Any) -> Any:
        return getattr(sql, f"{child_mode}_sql")(leaves, concurrency=3)

    result = observation.run(
        lambda: getattr(sql, f"{outer_mode}_sql")(
            [{"name": "parent", "type": "custom_sql_pipeline", "steps": [child]}],
            concurrency=1,
            soft_concurrency_cap=1,
            hard_concurrency_cap=1,
        )
    )
    assert result == {"parent": {str(index): str(index) for index in range(3)}}


@pytest.mark.parametrize("outer_mode", ["async", "parallel"])
@pytest.mark.parametrize("child_mode", ["async", "parallel"])
def test_nested_explicit_lower_hard_cap_is_checked_before_work_starts(
    monkeypatch: pytest.MonkeyPatch, outer_mode: str, child_mode: str
) -> None:
    calls: list[str] = []
    patch_task_operations(monkeypatch, mode=child_mode, read=lambda **kwargs: calls.append("read"))

    def child(_context: Any) -> Any:
        return getattr(sql, f"{child_mode}_sql")(
            [{"name": "leaf", "type": "read", "db_key": "gp", "query": "SELECT 1"}],
            concurrency=3,
            hard_concurrency_cap=1,
        )

    with pytest.raises(ValueError, match="hard_concurrency_cap"):
        getattr(sql, f"{outer_mode}_sql")(
            [{"name": "parent", "type": "custom_sql_pipeline", "steps": [child]}],
            concurrency=3,
            soft_concurrency_cap=3,
            hard_concurrency_cap=5,
        )
    assert calls == []
