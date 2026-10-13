"""Protect deterministic scheduling and intentional structural checks."""

from __future__ import annotations

import ast
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, BrokenBarrierError

import pytest

import tests._support.source as source_helpers
from tests._support.concurrency import ConcurrencyProbe, observe_call, wait_event
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

    excess = ConcurrencyProbe(2)
    all_active = Barrier(3)

    def worker() -> None:
        with excess.worker():
            all_active.wait(timeout=10)

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = [executor.submit(worker) for _ in range(3)]
        for future in futures:
            future.result(timeout=10)
    with pytest.raises(AssertionError, match="observed 3"):
        excess.assert_complete(calls=3)


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
