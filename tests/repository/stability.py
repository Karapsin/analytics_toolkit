"""Protect deterministic scheduling and intentional structural checks."""

from __future__ import annotations

import ast
import asyncio
import importlib
from concurrent.futures import ThreadPoolExecutor
from threading import BrokenBarrierError

import pytest

from tests._support import source as source_helpers
from tests._support.concurrency import (
    ConcurrencyProbe,
    SchedulingObservation,
    observe_call,
    wait_event,
)
from tests._support.paths import REPO_ROOT
from tests._support.source import (
    CodeRules,
    backend_branch_count,
    branches_on,
    called_names,
    contains_expression,
    forbidden_code,
    imported_modules,
    referenced_names,
)
from tests._support.stability import sensitive_calls
from tests.ab_utils._support.parallel import (
    ab_utils_module,
    bypass_metric_semaphores,
    observe_metric_cap,
    parallel_module,
    patch_metric_runner,
    pd,
)
from tests.sql._support.execute_read import observe_execute_cap
from tests.sql._support.tasks import (
    bypass_task_soft_cap,
    observe_task_cap,
    patch_task_operations,
)

sql = importlib.import_module("analytics_toolkit.sql")


def test_concurrency_probe_handles_partial_last_wave_and_worker_errors() -> None:
    probe = ConcurrencyProbe(2)

    def worker(index: int) -> int:
        with probe.worker():
            if index == 2:
                raise ValueError("worker failed")  # noqa: EM101, TRY003
            return index

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(worker, index) for index in range(5)]
        assert [future.result() for future in futures[:2]] == [0, 1]
        with pytest.raises(ValueError, match="worker failed"):
            futures[2].result()
        assert [future.result() for future in futures[3:]] == [3, 4]
    probe.assert_complete(calls=5)


def test_concurrency_probe_rejects_insufficient_and_excess_parallelism() -> None:
    insufficient = ConcurrencyProbe(2, timeout=0.01)
    with pytest.raises(BrokenBarrierError), insufficient.worker():
        pass
    with pytest.raises(AssertionError, match="did not rendezvous"):
        insufficient.assert_complete(calls=1)
    assert insufficient.active == 0

    excess = ConcurrencyProbe(2, held=True)
    observation = SchedulingObservation(excess, calls=3)
    executor_type = observation.executor_factory(ThreadPoolExecutor)

    def worker() -> None:
        with excess.worker():
            pass

    def consumer() -> None:
        with executor_type(max_workers=3) as executor:
            futures = [executor.submit(worker) for _ in range(3)]
            for future in futures:
                future.result(timeout=10)

    with pytest.raises(AssertionError, match="observed 3"):
        observation.run(consumer)
    assert excess.release.is_set()
    assert excess.active == 0
    assert excess.calls == 3


def test_held_probe_handles_partial_wave_and_propagates_worker_errors() -> None:
    probe = ConcurrencyProbe(2, held=True)
    observation = SchedulingObservation(probe, calls=5)
    executor_type = observation.executor_factory(ThreadPoolExecutor)
    error = ValueError("worker failed")

    def worker(index: int) -> int:
        with probe.worker():
            if index == 4:
                raise error
            return index

    def consumer() -> list[int]:
        with executor_type(max_workers=2) as executor:
            futures = [executor.submit(worker, index) for index in range(5)]
            return [future.result(timeout=10) for future in futures]

    with pytest.raises(ValueError, match="worker failed") as caught:
        observation.run(consumer)
    assert caught.value is error
    assert probe.release.is_set()
    probe.assert_complete(calls=5)


def test_held_probe_fails_insufficient_parallelism_and_releases_workers() -> None:
    probe = ConcurrencyProbe(2, held=True, timeout=0.1)
    observation = SchedulingObservation(probe, calls=3)
    executor_type = observation.executor_factory(ThreadPoolExecutor)

    def worker() -> None:
        with probe.worker():
            pass

    def consumer() -> None:
        with executor_type(max_workers=1) as executor:
            futures = [executor.submit(worker) for _ in range(3)]
            for future in futures:
                future.result(timeout=10)

    with pytest.raises(AssertionError, match="Initial scheduling wave"):
        observation.run(consumer)
    assert probe.release.is_set()
    assert probe.active == 0
    assert probe.calls == 3


@pytest.mark.parametrize("error_type", [LookupError, KeyboardInterrupt])
def test_held_probe_propagates_consumer_failure_before_workers_enter(error_type) -> None:
    probe = ConcurrencyProbe(2, held=True)
    observation = SchedulingObservation(probe, calls=3)
    error = error_type("consumer failed")

    def consumer() -> None:
        raise error

    with pytest.raises(error_type, match="consumer failed") as caught:
        observation.run(consumer)
    assert caught.value is error
    assert probe.release.is_set()
    assert probe.active == 0


def test_observed_call_preserves_arguments_results_and_errors(monkeypatch) -> None:
    class Primitive:
        def operation(self, value: str, *, fail: bool) -> str:
            if fail:
                raise LookupError(value)
            return value

    primitive = Primitive()
    failed = observe_call(monkeypatch, primitive, "operation", after_error=LookupError)
    assert primitive.operation("ok", fail=False) == "ok"
    assert not failed.is_set()
    with pytest.raises(LookupError, match="broken"):
        primitive.operation("broken", fail=True)
    wait_event(failed)

    # A completed but non-matching attempt must not wake the controller.
    successful = Primitive()
    returned = observe_call(monkeypatch, successful, "operation", after_result="blocked")
    assert successful.operation("ok", fail=False) == "ok"
    assert not returned.is_set()
    assert successful.operation("blocked", fail=False) == "blocked"
    wait_event(returned)


@pytest.mark.parametrize(
    ("after_error", "error_type", "signals"),
    [(LookupError, LookupError, True), (LookupError, ValueError, False), (None, ValueError, False)],
    ids=["matching-error", "unrelated-error", "result-only-error"],
)
def test_observed_call_preserves_exception_identity_and_restores_primitive(
    monkeypatch, after_error, error_type, signals
) -> None:
    error = error_type("primitive failed")
    value = object()
    calls = []

    class Primitive:
        def operation(self, argument, *, option):
            calls.append((argument, option))
            raise error

    primitive = Primitive()
    original = primitive.operation
    with monkeypatch.context() as patch:
        entered = observe_call(
            patch, primitive, "operation", after_error=after_error, after_result=False
        )
        with pytest.raises(error_type, match="primitive failed") as caught:
            primitive.operation(value, option=value)
        assert caught.value is error
        assert entered.is_set() is signals
        assert calls == [(value, value)]
    assert primitive.operation == original


@pytest.mark.parametrize("expression", ["backend == 'ch'", '(backend\n == "ch")'])
def test_structural_checks_ignore_layout_but_detect_actual_code(expression: str) -> None:
    tree = ast.parse(f"if {expression}:\n    adapter.execute_sql(connection, query)\n")
    assert contains_expression(tree, "backend == 'ch'")
    assert branches_on(tree, "backend")
    assert called_names(tree) == {"execute_sql"}
    assert "adapter" in referenced_names(tree)
    assert backend_branch_count(tree) == 1
    assert not contains_expression(tree, "backend != 'ch'")
    comments = ast.parse("# backend == 'ch'\n'backend == \\\"ch\\\"'\n")
    assert not contains_expression(comments, "backend == 'ch'")
    assert backend_branch_count(comments) == 0


@pytest.mark.parametrize("expression", ["'gp' != config.backend", "backend in ('gp', 'ch')"])
def test_backend_branch_checks_detect_reversed_and_membership_dispatch(expression: str) -> None:
    assert backend_branch_count(ast.parse(expression)) == 1


def test_import_checks_resolve_aliases_and_relative_paths_without_matching_comments(
    tmp_path,
) -> None:
    source = tmp_path / "caller.py"
    source.write_text(
        "# from .table_ops import removed\n"
        "from .table_ops import (removed as renamed,)\n"
        "import analytics_toolkit.sql.orchestration.async_api as old\n",
        encoding="utf-8",
    )
    modules = imported_modules(source, package="analytics_toolkit.sql.dml.table")
    assert "analytics_toolkit.sql.dml.table.table_ops" in modules
    assert "analytics_toolkit.sql.dml.table.table_ops.removed" in modules
    assert "analytics_toolkit.sql.orchestration.async_api" in modules
    source.write_text("# from .table_ops import removed\n", encoding="utf-8")
    assert imported_modules(source, package="analytics_toolkit.sql.dml.table") == set()


def test_ownership_checks_preserve_local_helpers_and_detect_actual_dependencies(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(source_helpers, "REPO_ROOT", tmp_path)
    source = tmp_path / "package" / "caller.py"
    source.parent.mkdir()
    source.write_text(
        '"""DROP PARTITION is documented here."""\n'
        "# frame.applymap(_normalize_ch_scalar)\n"
        "def _normalize_ch_scalar(value):\n    return value\n",
        encoding="utf-8",
    )
    rules = CodeRules(
        strings=("DROP PARTITION",),
        calls_with=(("applymap", "_normalize_ch_scalar"),),
        modules=("package.removed",),
    )
    assert forbidden_code(source, rules) == []
    source.write_text(
        "from .removed import (helper as renamed,)\n"
        "frame.applymap( _normalize_ch_scalar )\n"
        "sql = f'ALTER TABLE {table} DROP PARTITION {key}'\n",
        encoding="utf-8",
    )
    assert forbidden_code(source, rules) == [
        "DROP PARTITION",
        "applymap(_normalize_ch_scalar)",
        "package.removed",
    ]


def test_stability_audit_detects_aliases_and_keeps_nested_test_ownership() -> None:
    calls = sensitive_calls(
        "import time as clock\nfrom inspect import getsource as source\n"
        "def test_workers():\n"
        "    def worker():\n        clock.sleep(0.1)\n"
        "    source(worker)\n"
    )
    assert calls == [
        ("test_workers", "time.sleep", "0.1"),
        ("test_workers", "inspect.getsource", "expression"),
    ]


def test_offline_tests_use_signals_instead_of_scheduling_delays() -> None:
    # These two cases deliberately cross a real UI timer interval, not worker
    # scheduling. A changed duration or a new exception requires explicit review.
    timer_cases = {
        (
            "tests/sql/explorer/editor.py",
            "test_sql_editor_configuration_and_parser_failure_fallback",
        ): "0.6",
        (
            "tests/sql/explorer/overlays.py",
            "test_terminal_forwarded_command_o_alias_opens_navigation_mode",
        ): "1.1",
    }
    seen_timers = set()
    offenders = []
    for path in (REPO_ROOT / "tests").rglob("*.py"):
        if "_support" in path.parts or "integration" in path.parts:
            continue
        relative = path.relative_to(REPO_ROOT).as_posix()
        for owner, name, argument in sensitive_calls(path.read_text(encoding="utf-8")):
            if name == "asyncio.sleep" and argument == "0":
                continue  # Explicit event-loop yield has no scheduling delay.
            key = (relative, owner)
            if name == "asyncio.sleep" and timer_cases.get(key) == argument:
                seen_timers.add(key)
                continue
            offenders.append(f"{relative}::{owner}: {name}")
    assert offenders == [], "Use concurrency/source support helpers: " + "; ".join(offenders)
    # Removed timer cases need no exception entry.
    assert seen_timers == set(timer_cases)


@pytest.mark.parametrize(
    ("mode", "nested"),
    [("parallel", False), ("parallel", True), ("async", False)],
    ids=["thread-semaphore", "nested-leaves", "async-semaphore-and-executor"],
)
@pytest.mark.parametrize("bypass", [False, True], ids=["real-limiter", "ignored-limiter"])
def test_public_sql_cap_harness_rejects_ignored_soft_limiter(
    monkeypatch, mode, nested, bypass
) -> None:
    probe = ConcurrencyProbe(2, held=True)

    def read(**kwargs):
        with probe.worker():
            return kwargs["query"]

    tasks = [
        {"name": f"read_{index}", "type": "read", "db_key": "gp", "query": f"select {index}"}
        for index in range(6)
    ]

    def nested_batch(_context):
        return sql.parallel_sql(tasks, concurrency=6, progress=False)

    outer_tasks = (
        [{"name": "pipeline", "type": "custom_sql_pipeline", "steps": [nested_batch]}]
        if nested
        else tasks
    )
    operation = sql.async_sql if mode == "async" else sql.parallel_sql
    with monkeypatch.context() as patch:
        patch_task_operations(patch, mode=mode, read=read)
        if bypass:
            bypass_task_soft_cap(patch, mode=mode)
        observation = observe_task_cap(patch, probe, mode=mode, calls=6, nested=nested)

        def consume():
            return operation(
                outer_tasks,
                concurrency=1 if nested else 6,
                soft_concurrency_cap=2,
                hard_concurrency_cap=6,
                progress=False,
            )

        if bypass:
            with pytest.raises(AssertionError, match="observed 6"):
                observation.run(consume)
        else:
            result = observation.run(consume)
            leaves = result["pipeline"] if nested else result
            assert list(leaves) == [f"read_{index}" for index in range(6)]
            assert list(leaves.values()) == [f"select {index}" for index in range(6)]
            probe.assert_complete(calls=6)
    assert probe.release.is_set()
    assert probe.active == 0
    assert probe.calls == 6


@pytest.mark.parametrize(
    ("domain", "bypass"),
    [("metric", True), ("metric", False), ("execute", True)],
    ids=["metric-ignored-cap", "metric-real-semaphore", "execute-ignored-cap"],
)
def test_public_executor_cap_harness_observes_excess_capacity(monkeypatch, domain, bypass) -> None:
    probe = ConcurrencyProbe(2, held=True)

    module = (
        parallel_module
        if domain == "metric"
        else importlib.import_module("analytics_toolkit.sql.dml.io.execute_sql")
    )
    original_executor = module.ThreadPoolExecutor
    original_semaphore = getattr(module, "Semaphore", None)
    with monkeypatch.context() as patch:

        class OversizedExecutor(ThreadPoolExecutor):
            def __init__(self, *args, **kwargs) -> None:
                super().__init__(*args, **{**kwargs, "max_workers": 3})

        if domain == "metric":

            def metric(**kwargs):
                with probe.worker():
                    return pd.DataFrame({"metric_name": [kwargs["metric_name"]]})

            patch_metric_runner(patch, metric)
            if bypass:
                bypass_metric_semaphores(patch)
            patch.setattr(parallel_module, "ThreadPoolExecutor", OversizedExecutor)
            observation = observe_metric_cap(patch, probe, calls=3)
            tasks = {
                f"task_{index}": {"df": pd.DataFrame(), "metric_name": f"metric_{index}"}
                for index in range(3)
            }

            def operation():
                return ab_utils_module.compute_test_metrics(
                    tasks, concurrency=3, soft_concurrency_cap=2, progress=False
                )
        else:

            def execute(options):
                with probe.worker():
                    return options.source_sql

            patch.setattr(module, "_execute_sql_options", execute)
            patch.setattr(module, "ThreadPoolExecutor", OversizedExecutor)
            observation = observe_execute_cap(patch, probe, calls=3)

            def operation():
                return module.execute_sql(
                    "gp",
                    ["select 1", "select 2", "select 3"],
                    concurrency=3,
                    soft_concurrency_cap=2,
                )

        if bypass:
            with pytest.raises(AssertionError, match="observed 3"):
                observation.run(operation)
        else:
            result = observation.run(operation)
            assert list(result) == ["task_0", "task_1", "task_2"]
            probe.assert_complete(calls=3)
        assert probe.release.is_set()
        assert probe.active == 0
        assert probe.calls == 3
    assert module.ThreadPoolExecutor is original_executor
    assert getattr(module, "Semaphore", None) is original_semaphore


@pytest.mark.parametrize("mode", ["async", "parallel"])
@pytest.mark.parametrize("fail", [False, True], ids=["success", "worker-error"])
def test_task_cap_observation_restores_scheduling_patches(monkeypatch, mode, fail) -> None:
    module = importlib.import_module(f"analytics_toolkit.sql.orchestration.{mode}_sql")
    semaphore_module = asyncio if mode == "async" else module
    original_executor = module.ThreadPoolExecutor
    original_semaphore = semaphore_module.Semaphore
    original_read = module.read_sql
    probe = ConcurrencyProbe(2, held=True)
    error = LookupError("worker failed")

    def read(**kwargs):
        with probe.worker():
            if fail and kwargs["query"] == "select 2":
                raise error
            return kwargs["query"]

    tasks = [
        {"name": f"read_{index}", "type": "read", "db_key": "gp", "query": f"select {index}"}
        for index in range(3)
    ]
    with monkeypatch.context() as patch:
        patch_task_operations(patch, mode=mode, read=read)
        observation = observe_task_cap(patch, probe, mode=mode, calls=3)

        def operation():
            return getattr(sql, f"{mode}_sql")(
                tasks, concurrency=3, soft_concurrency_cap=2, progress=False
            )

        if fail:
            with pytest.raises(LookupError, match="worker failed") as caught:
                observation.run(operation)
            assert caught.value is error
        else:
            result = observation.run(operation)
            assert list(result) == ["read_0", "read_1", "read_2"]
        assert module.ThreadPoolExecutor is original_executor
        assert semaphore_module.Semaphore is original_semaphore
    assert module.read_sql is original_read
    assert probe.release.is_set()
    probe.assert_complete(calls=3)
