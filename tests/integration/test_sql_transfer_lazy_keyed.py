from __future__ import annotations

import datetime as dt
import importlib
from typing import TYPE_CHECKING, Any

import pandas as pd
import pytest
from analytics_toolkit import sql
from tests.integration.manifest import scenario_param
from tests.integration.support.backends import (
    BACKENDS,
    backend_alias,
    backend_enabled,
    integration_table,
    table_options,
)
from tests.integration.support.normalization import assert_exact_frame

if TYPE_CHECKING:
    from tests.integration.support.resources import ResourceRegistry

pytestmark = pytest.mark.integration


def _register_table(
    registry: ResourceRegistry,
    backend: str,
    alias: str,
    purpose: str,
) -> str:
    return registry.table(
        alias,
        integration_table(backend, purpose),
        ch_cluster="integration_cluster" if backend == "ch" else None,
    )


def _transfer_options(backend: str) -> dict[str, object]:
    options = table_options(backend)
    if backend == "ch":
        options["table_schema"] = {
            "row_id": "Int64",
            "slice_key": "Int64",
            "event_date": "Date",
            "value": "String",
        }
    return options


def _attempt_stage_tables(alias: str, backend: str, transfer_id: str) -> pd.DataFrame:
    schema = {
        "gp": "public",
        "trino": "integration_stage",
        "ch": "integration",
    }[backend]
    options: dict[str, Any] = {
        "schema": schema,
        "conditions": f"table_name LIKE '%{transfer_id}%'",
    }
    if backend == "trino":
        options["trino_catalog"] = "iceberg"
    return sql.show_tables(alias, **options)


def _assert_transfer_result(
    result: Any,
    *,
    expected: pd.DataFrame,
    target_alias: str,
    target_table: str,
) -> None:
    assert result.rows == len(expected)
    metadata = result.metadata
    assert metadata.row_count_validated is True
    assert metadata.transfer_slice_counts is not None
    assert [
        (item["expected_rows"], item["streamed_rows"]) for item in metadata.transfer_slice_counts
    ] == [(2, 2), (0, 0), (2, 2)]
    actual = sql.read(
        target_alias,
        f"SELECT row_id, slice_key, event_date, value FROM {target_table} ORDER BY row_id",
    )
    assert_exact_frame(actual, expected, date_columns=("event_date",))


@pytest.mark.parametrize(
    "backend",
    [scenario_param(f"transfer.lazy_keyed.{backend}", backend) for backend in BACKENDS],
)
@pytest.mark.integration_core
def test_lazy_keyed_source_staging_serial_and_capped_concurrent(
    backend: str,
    resource_registry: ResourceRegistry,
    capsys: pytest.CaptureFixture[str],
) -> None:
    if not backend_enabled(backend):
        pytest.skip("Greenplum integration runs only on x86_64")

    source_alias = backend_alias(backend)
    target_alias = backend_alias(backend, target=True)
    source_table = _register_table(
        resource_registry,
        backend,
        source_alias,
        "lazy_keyed_source",
    )
    serial_target = _register_table(
        resource_registry,
        backend,
        target_alias,
        "lazy_keyed_serial",
    )
    concurrent_target = _register_table(
        resource_registry,
        backend,
        target_alias,
        "lazy_keyed_concurrent",
    )
    source = pd.DataFrame(
        {
            "row_id": [1, 2, 3, 4],
            "slice_key": [1, 1, 2, 2],
            "event_date": [
                dt.date(2026, 8, 1),
                dt.date(2026, 8, 1),
                dt.date(2026, 8, 2),
                dt.date(2026, 8, 2),
            ],
            "value": ["one-a", "one-b", "two-a", "two-b"],
        }
    )
    sql.load_df(
        source_alias,
        source_table,
        source,
        write_mode="replace",
        **table_options(backend),
    )

    common: dict[str, Any] = {
        "from_table": source_table,
        "write_mode": "replace",
        "batch_size": 1,
        "adaptive_batch_size": False,
        "target_rows_per_second": False,
        "retry_cnt": 1,
        "timeout_increment": 0,
        "full_retry_cnt": 1,
        "full_timeout_increment": 0,
        "transfer_keys": "slice_key",
        "transfer_key_values": [1, 99, 2],
        "return_metadata": True,
        **_transfer_options(backend),
    }

    serial_result = sql.transfer(
        source_alias,
        target_alias,
        to_table=serial_target,
        concurrency=1,
        **common,
    )
    assert isinstance(serial_result, sql.SqlOperationResult)
    _assert_transfer_result(
        serial_result,
        expected=source,
        target_alias=target_alias,
        target_table=serial_target,
    )
    assert serial_result.metadata.effective_read_concurrency == 1
    assert serial_result.metadata.effective_write_concurrency == 1
    capsys.readouterr()

    concurrent_result = sql.transfer(
        source_alias,
        target_alias,
        to_table=concurrent_target,
        read_concurrency=4,
        write_concurrency=4,
        soft_concurrency_cap=2,
        hard_concurrency_cap=4,
        **common,
    )
    transfer_output = capsys.readouterr().out
    assert isinstance(concurrent_result, sql.SqlOperationResult)
    _assert_transfer_result(
        concurrent_result,
        expected=source,
        target_alias=target_alias,
        target_table=concurrent_target,
    )
    assert concurrent_result.metadata.requested_read_concurrency == 4
    assert concurrent_result.metadata.requested_write_concurrency == 4
    assert concurrent_result.metadata.soft_limited_read_concurrency == 2
    assert concurrent_result.metadata.soft_limited_write_concurrency == 2
    assert concurrent_result.metadata.effective_read_concurrency == 2
    assert concurrent_result.metadata.effective_write_concurrency == 2

    batch_lines = [line for line in transfer_output.splitlines() if " Staged batch " in line]
    assert len(batch_lines) == len(source)
    assert all(line.index("[slice=") < line.index("Staged batch") for line in batch_lines)
    assert any("[slice=1/3 key=slice_key:1] Staged batch" in line for line in batch_lines)
    assert any("[slice=3/3 key=slice_key:2] Staged batch" in line for line in batch_lines)
    assert all("batch rate " in line and "rows/s" in line for line in batch_lines)
    rolling_lines = [line for line in batch_lines if "rolling rate unavailable" not in line]
    assert rolling_lines
    assert all(
        line.split("rolling rate ", 1)[1].split(";", 1)[0].endswith("rows/s")
        for line in rolling_lines
    )
    assert all(
        line.split("approximate RAM rate ", 1)[1].split(";", 1)[0].endswith("B/s")
        for line in batch_lines
    )
    rolling_memory_lines = [
        line for line in batch_lines if "rolling approximate RAM rate unavailable" not in line
    ]
    assert rolling_memory_lines
    assert all(
        line.split("rolling approximate RAM rate ", 1)[1].split(";", 1)[0].endswith("B/s")
        for line in rolling_memory_lines
    )
    eta_lines = [
        line
        for line in batch_lines
        if "load ETA unavailable" not in line and "total transfer ETA unavailable" not in line
    ]
    assert eta_lines
    assert all(line.index("load ETA ") < line.index("total transfer ETA ") for line in eta_lines)
    for completion in (
        "Completed source-stage loading:",
        "Completed target-stage consolidation:",
        "Starting destination finalization:",
        "Completed destination finalization:",
        "Completed transfer:",
        "source stages dropped 3/3",
        "target stages cleaned",
    ):
        assert completion in transfer_output

    for result in (serial_result, concurrent_result):
        transfer_id = result.metadata.transfer_id
        assert transfer_id
        source_stages = _attempt_stage_tables(source_alias, backend, transfer_id)
        target_stages = _attempt_stage_tables(target_alias, backend, transfer_id)
        assert source_stages.empty, source_stages["table_name"].tolist()
        assert target_stages.empty, target_stages["table_name"].tolist()


@pytest.mark.parametrize(
    "backend",
    [scenario_param(f"fault.lazy_keyed.drop.{backend}", backend) for backend in BACKENDS],
)
@pytest.mark.integration_fault
def test_lazy_keyed_full_retry_after_acknowledged_source_drop(
    backend: str,
    resource_registry: ResourceRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not backend_enabled(backend):
        pytest.skip("Greenplum integration runs only on x86_64")

    source_alias = backend_alias(backend)
    target_alias = backend_alias(backend, target=True)
    source_table = _register_table(
        resource_registry,
        backend,
        source_alias,
        "lazy_keyed_retry_source",
    )
    target_table = _register_table(
        resource_registry,
        backend,
        target_alias,
        "lazy_keyed_retry_target",
    )
    source = pd.DataFrame(
        {
            "row_id": [1, 2, 3, 4],
            "slice_key": [1, 1, 2, 2],
            "event_date": [
                dt.date(2026, 8, 1),
                dt.date(2026, 8, 1),
                dt.date(2026, 8, 2),
                dt.date(2026, 8, 2),
            ],
            "value": ["one-a", "one-b", "two-a", "two-b"],
        }
    )
    sql.load_df(
        source_alias,
        source_table,
        source,
        write_mode="replace",
        **table_options(backend),
    )

    pipeline = importlib.import_module(
        "analytics_toolkit.sql.dml.transfer.flow.staged_keyed_pipeline"
    )
    real_materialize = pipeline.materialize_source_key
    real_drop = pipeline.drop_source_stage
    real_drain = pipeline._drain_drop_ready
    materialized_by_attempt: dict[int, list[int]] = {}
    acknowledged_drops: list[tuple[int, int, str]] = []
    injected = False

    def record_materialization(
        options: Any,
        source_ref: Any,
        metadata: Any,
        transfer_slice: Any,
        source_stage: str,
    ) -> int:
        rows = real_materialize(
            options,
            source_ref,
            metadata,
            transfer_slice,
            source_stage,
        )
        materialized_by_attempt.setdefault(options.attempt_number, []).append(transfer_slice.index)
        return rows

    def record_acknowledged_drop(options: Any, source_ref: Any, task: Any) -> None:
        real_drop(options, source_ref, task)
        acknowledged_drops.append(
            (options.attempt_number, task.transfer_slice.index, task.source_stage)
        )

    def fail_after_acknowledged_drop(
        options: Any,
        runtime: Any,
        source_connections: Any,
        *,
        limit: int | None,
    ) -> int:
        nonlocal injected
        dropped = real_drain(
            options,
            runtime,
            source_connections,
            limit=limit,
        )
        if options.attempt_number == 1 and dropped and not injected:
            injected = True
            message = "injected failure after an acknowledged source stage was dropped"
            raise OSError(message)
        return dropped

    monkeypatch.setattr(pipeline, "materialize_source_key", record_materialization)
    monkeypatch.setattr(pipeline, "drop_source_stage", record_acknowledged_drop)
    monkeypatch.setattr(pipeline, "_drain_drop_ready", fail_after_acknowledged_drop)

    result = sql.transfer(
        source_alias,
        target_alias,
        from_table=source_table,
        to_table=target_table,
        write_mode="replace",
        batch_size=1,
        adaptive_batch_size=False,
        target_rows_per_second=False,
        retry_cnt=1,
        timeout_increment=0,
        full_retry_cnt=2,
        full_timeout_increment=0,
        transfer_keys="slice_key",
        transfer_key_values=[1, 99, 2],
        concurrency=1,
        return_metadata=True,
        **_transfer_options(backend),
    )

    assert isinstance(result, sql.SqlOperationResult)
    _assert_transfer_result(
        result,
        expected=source,
        target_alias=target_alias,
        target_table=target_table,
    )
    assert injected is True
    assert set(materialized_by_attempt) == {1, 2}
    assert sorted(materialized_by_attempt[2]) == [0, 1, 2]
    first_attempt_drops = [item for item in acknowledged_drops if item[0] == 1]
    assert first_attempt_drops
    assert all(stage_table for _attempt, _slice, stage_table in first_attempt_drops)
    first_attempt_drop_slices = {
        slice_index for _attempt, slice_index, _stage in first_attempt_drops
    }
    assert first_attempt_drop_slices <= set(materialized_by_attempt[1])
    assert first_attempt_drop_slices <= set(materialized_by_attempt[2])

    transfer_id = result.metadata.transfer_id
    assert transfer_id
    source_stages = _attempt_stage_tables(source_alias, backend, transfer_id)
    target_stages = _attempt_stage_tables(target_alias, backend, transfer_id)
    assert source_stages.empty, source_stages["table_name"].tolist()
    assert target_stages.empty, target_stages["table_name"].tolist()
