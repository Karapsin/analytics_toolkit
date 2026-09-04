from __future__ import annotations

import asyncio
from threading import Event
from types import SimpleNamespace

import pytest
from analytics_toolkit.sql_explorer.completion import (
    CompletionCoordinator,
    CompletionCoordinatorPool,
    CompletionRequest,
)
from analytics_toolkit.sql_explorer.runtime import DatabaseSelection
from analytics_toolkit.sql_explorer.scheduling import ExplorerQueryScheduler
from analytics_toolkit.sql_explorer.statements import build_execution_plan
from analytics_toolkit.sql_explorer.workspace import SqlExplorerWorkspace, workspace_for

from tests.sql.explorer.app import FakeSession


def test_user_query_scheduler_is_fifo_and_limits_each_tab_to_one_job() -> None:
    scheduler = ExplorerQueryScheduler()
    database = DatabaseSelection("gp", "gp")
    first_plan = build_execution_plan("select 1", "gp")
    second_plan = build_execution_plan("select 2", "gp")

    first = scheduler.enqueue("first", first_plan, database)
    second = scheduler.enqueue("second", second_plan, database)

    assert first is not None
    assert second is not None
    assert scheduler.enqueue("first", second_plan, database) is None
    assert scheduler.position("first") == 1
    assert scheduler.position("second") == 2
    assert scheduler.take_startable() == (first,)
    assert scheduler.active_count == 1
    assert scheduler.position("second") == 1

    assert scheduler.complete(first.job_id) == first
    assert scheduler.is_active(first.job_id) is False
    assert scheduler.take_startable() == (second,)
    assert second is not None
    assert scheduler.is_active(second.job_id) is True
    assert scheduler.position("missing") is None


def test_query_scheduler_snapshots_database_and_supports_safe_concurrency_changes() -> None:
    scheduler = ExplorerQueryScheduler()
    first_database = DatabaseSelection("gp", "gp")
    second_database = DatabaseSelection("lake", "trino")
    plan = build_execution_plan("select 1", "gp")
    first = scheduler.enqueue("first", plan, first_database)
    second = scheduler.enqueue("second", plan, second_database)

    scheduler.set_concurrency(2)

    assert scheduler.take_startable() == (first, second)
    assert first is not None
    assert first.database == first_database
    assert second is not None
    assert second.database == second_database
    scheduler.set_concurrency(1)
    assert scheduler.active_count == 2


def test_removing_a_tab_drops_only_its_pending_query() -> None:
    scheduler = ExplorerQueryScheduler()
    database = DatabaseSelection("gp", "gp")
    plan = build_execution_plan("select 1", "gp")
    first = scheduler.enqueue("first", plan, database)
    second = scheduler.enqueue("second", plan, database)
    third = scheduler.enqueue("third", plan, database)

    assert scheduler.take_startable() == (first,)
    assert scheduler.remove_pending_tab("second") == second
    assert scheduler.pending_count == 1
    assert scheduler.job_for_tab("first") == first
    assert scheduler.job_for_tab("third") == third


@pytest.mark.parametrize("value", [0, True, "1"])
def test_query_scheduler_rejects_invalid_concurrency(value: object) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        ExplorerQueryScheduler(value)  # type: ignore[arg-type]


class _BlockingMetadataProvider:
    def __init__(self) -> None:
        self.started = Event()
        self.release = Event()

    def list_tables(self, **_kwargs: object) -> tuple[str, ...]:
        self.started.set()
        assert self.release.wait(timeout=2)
        return ("sample_table",)

    def list_catalogs(self, **_kwargs: object) -> tuple[str, ...]:
        return ()

    def list_schemas(self, **_kwargs: object) -> tuple[str, ...]:
        return ()


def test_metadata_queue_fans_out_same_request_and_removes_closed_owner() -> None:
    provider = _BlockingMetadataProvider()
    coordinator = CompletionCoordinator("gp", "gp", provider=provider)
    request = CompletionRequest("gp", "gp", "table", "sample", context="from:0")
    first_results: list[object] = []
    second_results: list[object] = []
    completed = Event()
    try:
        coordinator.enqueue(
            request,
            owner_id="first",
            on_success=first_results.append,
        )
        assert provider.started.wait(timeout=2)
        coordinator.enqueue(
            request,
            owner_id="second",
            on_success=lambda result: (second_results.append(result), completed.set()),
        )
        coordinator.remove_owner("first")
        provider.release.set()

        assert completed.wait(timeout=2)
        assert first_results == []
        assert len(second_results) == 1
    finally:
        provider.release.set()
        coordinator.stop()


def test_metadata_owner_removal_keeps_shared_queued_work_and_drops_orphans() -> None:
    provider = _BlockingMetadataProvider()
    coordinator = CompletionCoordinator("gp", "gp", provider=provider)
    running = CompletionRequest("gp", "gp", "table", "running", context="from:0")
    shared = CompletionRequest("gp", "gp", "table", "shared", context="from:1")
    orphan = CompletionRequest("gp", "gp", "table", "orphan", context="from:2")
    shared_results: list[object] = []
    completed = Event()
    try:
        coordinator.enqueue(running, owner_id="running")
        assert provider.started.wait(timeout=2)
        coordinator.enqueue(shared, owner_id="closing")
        coordinator.enqueue(
            shared,
            owner_id="remaining",
            on_success=lambda result: (shared_results.append(result), completed.set()),
        )
        coordinator.enqueue(orphan, owner_id="closing")

        coordinator.remove_owner("closing")
        assert coordinator.snapshot()[1] == 1
        provider.release.set()

        assert completed.wait(timeout=2)
        assert len(shared_results) == 1
    finally:
        provider.release.set()
        coordinator.stop()


def test_metadata_pool_is_shared_by_alias_and_separate_between_databases() -> None:
    pool = CompletionCoordinatorPool()
    try:
        gp_first = pool.acquire("gp", "gp", "first")
        gp_second = pool.acquire("GP", "gp", "second")
        lake = pool.acquire("lake", "trino", "third")

        assert gp_first is gp_second
        assert lake is not gp_first
        assert set(pool.snapshot()) == {"gp", "lake"}
        pool.release("missing", "missing")
        pool.release("missing")
        pool.release("first", "gp")
        assert pool.coordinator_for("gp") is gp_second
        pool.release("second", "gp")
        assert pool.coordinator_for("gp") is None
    finally:
        pool.stop()


def test_workspace_helpers_cover_unmounted_and_attached_nodes() -> None:
    async def exercise() -> None:
        workspace = SqlExplorerWorkspace("test", 1, FakeSession())
        assert workspace.is_clean_untitled is True
        assert workspace_for(workspace) is workspace

        child = SimpleNamespace(ancestors=[workspace])
        assert workspace.contains(child) is True
        assert workspace_for(child) is workspace
        assert workspace.contains(None) is False
        with pytest.raises(RuntimeError, match="not attached"):
            workspace_for(SimpleNamespace(ancestors=[]))

    asyncio.run(exercise())
