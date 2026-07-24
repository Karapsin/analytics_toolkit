from __future__ import annotations

# ruff: noqa: B009, I001, PT011, PT018, RUF043, TC002

import asyncio
import importlib
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Callable

import pandas as pd
import pytest
from analytics_toolkit import sql
from tests.integration.manifest import scenario_param
from tests.integration.support.backends import (
    backend_enabled,
    integration_table,
)
from tests.integration.support.identity import query_label
from tests.integration.support.orchestration import Timeline
from tests.integration.support.query_workers import (
    QueryWorker,
    find_labelled_query,
    long_running_query,
    poll_until,
)
from tests.integration.support.resources import ResourceRegistry

pytestmark = [pytest.mark.integration, pytest.mark.integration_core]
APIS = ("parallel", "async")


def _label(purpose: str) -> str:
    return query_label(
        os.environ.get("SQL_INTEGRATION_RUN_ID", uuid.uuid4().hex[:8]),
        os.environ.get("SQL_INTEGRATION_TEST_ID", "manual"),
        purpose,
    )


def _query_is_gone(db_key: str, label: str) -> bool:
    active = sql.show_queries(db_key, state="active", retry_cnt=1)
    if active.empty:
        return True
    return not active["query"].astype(str).str.contains(label, regex=False).any()


def _runner(api: str) -> Callable[..., dict[str, Any]]:
    return sql.parallel_sql if api == "parallel" else sql.async_sql


def _table(registry: ResourceRegistry, purpose: str) -> str:
    return registry.table(
        "ch",
        integration_table("ch", purpose),
    )


@pytest.mark.parametrize(
    "api",
    [scenario_param(f"orchestration.{api}.task_types", api) for api in APIS],
)
def test_every_orchestration_task_type_returns_real_results(
    api: str,
    resource_registry: ResourceRegistry,
) -> None:
    runner = _runner(api)
    source = _table(resource_registry, f"{api}_source")
    execute_target = _table(resource_registry, f"{api}_execute")
    load_target = _table(resource_registry, f"{api}_load")
    transfer_target = _table(resource_registry, f"{api}_transfer")
    frame = pd.DataFrame(
        {
            "row_id": [1, 2],
            "event_date": [pd.Timestamp("2026-06-01"), pd.Timestamp("2026-06-02")],
            "value": ["one", "two"],
        }
    )
    sql.load_df("ch", source, frame, write_mode="replace", ch_engine="MergeTree", order_by="row_id")
    sql.create_sql_table(
        "ch",
        execute_target,
        table_schema={"row_id": "Int64"},
        ch_engine="MergeTree",
        order_by="row_id",
        ch_shard_on_cluster="integration_cluster",
        ch_distributed_on_cluster="integration_cluster",
        ch_distributed_cluster="integration_cluster",
        ch_only_shard=True,
    )

    def pipeline_step(context: Any) -> int:
        assert context.task_name == "pipeline"
        return int(sql.read("trino", "SELECT 6 AS value").iloc[0, 0])

    tasks = [
        {"name": "read", "type": "read", "db_key": "trino", "query": "SELECT 1 AS value"},
        {
            "name": "execute",
            "type": "execute",
            "db_key": "ch",
            "query": f"INSERT INTO {execute_target} VALUES (2)",
        },
        {
            "name": "execute_read",
            "type": "execute_read",
            "db_key": "ch",
            "query": "SELECT 3 AS value",
        },
        {
            "name": "load",
            "type": "load_df",
            "db_key": "ch",
            "destination_table": load_target,
            "df": frame,
            "write_mode": "replace",
            "ch_engine": "MergeTree",
            "order_by": "row_id",
            "ch_shard_on_cluster": "integration_cluster",
            "ch_distributed_on_cluster": "integration_cluster",
            "ch_distributed_cluster": "integration_cluster",
            "ch_only_shard": True,
        },
        {
            "name": "transfer",
            "type": "transfer",
            "from_db": "ch",
            "to_db": "trino_target_values",
            "from_sql": f"SELECT * FROM {source}",
            "to_table": transfer_target.replace("integration.", "iceberg.integration."),
            "write_mode": "replace",
            "batch_size": 1,
            "adaptive_batch_size": False,
            "target_rows_per_second": False,
            "partition_by": ["event_date"],
            "retry_cnt": 1,
            "full_retry_cnt": 1,
        },
        {"name": "pipeline", "type": "custom_sql_pipeline", "steps": [pipeline_step]},
    ]
    trino_target = tasks[4]["to_table"]
    assert isinstance(trino_target, str)
    resource_registry.table("trino_target_values", trino_target)

    results = runner(
        tasks,
        concurrency=3,
        start_comment="/* integration orchestration */",
        progress=True,
    )
    assert list(results) == [
        "read",
        "execute",
        "execute_read",
        "load",
        "transfer",
        "pipeline",
    ]
    assert int(results["read"].iloc[0, 0]) == 1
    assert results["execute"] == "success"
    assert int(results["execute_read"].iloc[0, 0]) == 3
    assert results["load"] == len(frame)
    assert results["transfer"] == len(frame)
    assert results["pipeline"] == 6

    unnamed = runner(
        [
            {"type": "read", "db_key": "trino", "query": "SELECT 7 AS value"},
            {"type": "read", "db_key": "ch", "query": "SELECT 8 AS value"},
        ],
        concurrency=1,
    )
    assert list(unnamed) == ["task_0", "task_1"]
    assert [int(value.iloc[0, 0]) for value in unnamed.values()] == [7, 8]


@pytest.mark.parametrize(
    "api",
    [scenario_param(f"orchestration.{api}.overlap", api) for api in APIS],
)
def test_pipeline_and_standard_sql_task_really_overlap(
    api: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timeline = Timeline()
    module = importlib.import_module(f"analytics_toolkit.sql.orchestration.{api}_sql")
    real_read = module._SYNC_TASK_RUNNERS["read"]

    def observed_read(kwargs: dict[str, Any]) -> Any:
        return timeline.step("standard", lambda: real_read(kwargs))

    def pipeline_step(context: Any) -> int:
        assert context.task_name == "pipeline"
        result = timeline.step(
            "pipeline",
            lambda: sql.read("ch", "SELECT 22 AS value", retry_cnt=1),
        )
        return int(result.iloc[0, 0])

    monkeypatch.setitem(module._SYNC_TASK_RUNNERS, "read", observed_read)
    results = _runner(api)(
        [
            {
                "name": "standard",
                "type": "read",
                "db_key": "trino",
                "query": "SELECT 21 AS value",
                "retry_cnt": 1,
            },
            {
                "name": "pipeline",
                "type": "custom_sql_pipeline",
                "steps": [pipeline_step],
            },
        ],
        concurrency=2,
    )
    assert int(results["standard"].iloc[0, 0]) == 21
    assert results["pipeline"] == 22
    timeline.assert_overlap("standard", "pipeline")
    artifact = Path(os.environ["SQL_INTEGRATION_ARTIFACT_DIR"])
    (artifact / "orchestration-timeline.json").write_text(
        json.dumps(timeline.events, indent=2),
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    "api",
    [scenario_param(f"orchestration.{api}.caps", api) for api in APIS],
)
def test_orchestration_hard_cap_rejects_before_task_start(api: str) -> None:
    started: list[str] = []

    def step(context: Any) -> None:
        started.append(context.task_name)

    with pytest.raises(ValueError, match="hard.*cap|concurrency") as exc_info:
        _runner(api)(
            [{"name": "must_not_start", "type": "custom_sql_pipeline", "steps": [step]}],
            concurrency=3,
            soft_concurrency_cap=2,
            hard_concurrency_cap=1,
        )
    assert "1" in str(exc_info.value) and "2" in str(exc_info.value)
    assert started == []


@pytest.mark.parametrize(
    "api",
    [scenario_param(f"orchestration.{api}.failure", api) for api in APIS],
)
def test_orchestration_failure_modes_keep_diagnostics_and_order(api: str) -> None:
    tasks = [
        {"name": "success", "type": "read", "db_key": "trino", "query": "SELECT 9"},
        {
            "name": "failure",
            "type": "read",
            "db_key": "trino",
            "query": "SELECT missing",
            "retry_cnt": 1,
        },
        {"name": "success_after", "type": "read", "db_key": "ch", "query": "SELECT 10"},
    ]
    retained = _runner(api)(tasks, concurrency=2, fail_fast=False)
    assert list(retained) == ["success", "failure", "success_after"]
    assert int(retained["success"].iloc[0, 0]) == 9
    assert isinstance(retained["failure"], str) and retained["failure"]
    assert int(retained["success_after"].iloc[0, 0]) == 10

    with pytest.raises(Exception) as exc_info:
        _runner(api)(tasks, concurrency=1, fail_fast=True)
    error = exc_info.value
    assert getattr(error, "analytics_toolkit_sql_task_name") == "failure"
    assert getattr(error, "analytics_toolkit_sql_task_type") == "read"
    assert getattr(error, "analytics_toolkit_sql_field") == "query"
    assert "SELECT missing" in getattr(error, "analytics_toolkit_sql_query")


@pytest.mark.parametrize(
    ("backend", "inside_event_loop"),
    [
        scenario_param("orchestration.async.interrupt.gp", "gp", True),
        scenario_param("orchestration.async.interrupt.trino", "trino", True),
        scenario_param("orchestration.async.interrupt.ch", "ch", True),
        scenario_param("orchestration.async.interrupt.direct", "ch", False),
    ],
)
def test_async_sql_interrupt_cancels_only_its_batch(
    backend: str,
    inside_event_loop: bool,
    resource_registry: ResourceRegistry,
) -> None:
    if not backend_enabled(backend):
        pytest.skip("Greenplum requires x86_64")

    table = resource_registry.table(
        backend,
        integration_table(backend, "async_interrupt_queue"),
    )
    load_kwargs: dict[str, Any] = {}
    if backend == "gp":
        load_kwargs["gp_distributed_by_key"] = "row_id"
    elif backend == "ch":
        load_kwargs.update(
            ch_engine="MergeTree",
            order_by="row_id",
            ch_shard_on_cluster="integration_cluster",
            ch_distributed_on_cluster="integration_cluster",
            ch_distributed_cluster="integration_cluster",
            ch_only_shard=True,
        )
    sql.load_df(
        backend,
        table,
        pd.DataFrame({"row_id": [1]}),
        write_mode="replace",
        retry_cnt=1,
        **load_kwargs,
    )

    unrelated_label = _label("async_interrupt_unrelated")
    unrelated = resource_registry.worker(
        QueryWorker(
            backend,
            long_running_query(backend),
            unrelated_label,
        ).start()
    )
    unrelated_row = find_labelled_query(backend, unrelated_label)
    unrelated.query_id = resource_registry.query(backend, unrelated_row["query_id"])

    interrupted_label = _label("async_interrupt_owned")
    interrupt_error = KeyboardInterrupt("integration interrupt")

    def interrupt_when_visible(_context: Any) -> None:
        row = find_labelled_query(backend, interrupted_label)
        resource_registry.query(backend, row["query_id"])
        raise interrupt_error

    tasks = [
        {
            "name": "running",
            "type": "read",
            "db_key": backend,
            "query": long_running_query(backend),
            "query_label": interrupted_label,
            "retry_cnt": 1,
        },
        {
            "name": "interrupt",
            "type": "custom_sql_pipeline",
            "steps": [interrupt_when_visible],
        },
        {
            "name": "queued_side_effect",
            "type": "execute",
            "db_key": backend,
            "query": f"INSERT INTO {table} (row_id) VALUES (2)",
            "retry_cnt": 1,
        },
    ]

    def run_batch() -> None:
        sql.async_sql(tasks, concurrency=2)

    async def run_batch_inside_event_loop() -> None:
        run_batch()

    def invoke_batch() -> None:
        if inside_event_loop:
            asyncio.run(run_batch_inside_event_loop())
        else:
            run_batch()

    started_at = time.monotonic()
    with pytest.raises(KeyboardInterrupt, match="integration interrupt"):
        invoke_batch()
    assert time.monotonic() - started_at < 12

    poll_until(
        lambda: _query_is_gone(backend, interrupted_label),
        description=f"interrupted async query to stop: {interrupted_label}",
    )
    assert not _query_is_gone(backend, unrelated_label)
    rows = sql.read(backend, f"SELECT count(*) AS row_count FROM {table}", retry_cnt=1)
    assert int(rows.iloc[0, 0]) == 1
