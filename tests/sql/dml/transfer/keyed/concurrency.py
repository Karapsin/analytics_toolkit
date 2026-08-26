from __future__ import annotations

from tests.sql._support.transfer_keyed import (
    LazyKeyedRuntime,
    SimpleNamespace,
    _Manager,
    _options,
    pytest,
    staged_keyed_pipeline,
)


def test_collect_worker_and_live_stage_credit_propagate_cancellation() -> None:
    runtime = LazyKeyedRuntime([], read_workers=1, write_workers=1)
    failure = RuntimeError("worker failed")
    future = SimpleNamespace(result=lambda: (_ for _ in ()).throw(failure))

    staged_keyed_pipeline._collect_workers([SimpleNamespace(result=lambda: None), future], runtime)

    assert runtime.first_error is failure
    runtime.cancellation.set()
    with pytest.raises(RuntimeError, match="scheduling was cancelled"):
        staged_keyed_pipeline._acquire_live_stage_credit(_options(), runtime, _Manager())
