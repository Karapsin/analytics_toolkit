from __future__ import annotations

import os
import uuid

import pandas as pd
import pytest
from analytics_toolkit import sql

pytestmark = [pytest.mark.integration, pytest.mark.integration_core]
BACKENDS = ("gp", "trino", "ch")
WRITE_MODES = ("append", "replace", "truncate_insert", "upsert")


def _enabled(backend: str) -> bool:
    return backend != "gp" or os.environ.get("SQL_INTEGRATION_GP") == "1"


def _alias(backend: str, *, target: bool = False) -> str:
    if backend == "gp":
        return "gp_alias" if target else "gp"
    if backend == "trino":
        return "trino_values" if target else "trino_parquet"
    return "ch" if target else "ch_limited"


def _table(backend: str, label: str) -> str:
    suffix = uuid.uuid4().hex[:10]
    if backend == "gp":
        return f"public.it_{label}_{suffix}"
    if backend == "trino":
        return f"iceberg.integration.it_{label}_{suffix}"
    return f"integration.it_{label}_{suffix}"


def _shape_options(backend: str) -> dict[str, object]:
    if backend == "gp":
        return {"gp_distributed_by_key": "id"}
    if backend == "trino":
        return {"partition_by": ["dt"]}
    return {
        "partition_by": ["dt"],
        "order_by": ["id"],
        "ch_cluster": "integration_cluster",
    }


def _upsert_options(backend: str) -> dict[str, object]:
    options: dict[str, object] = {"key_columns": ["id"]}
    if backend != "gp":
        options["upsert_partition_column"] = "dt"
    return options


def _frame(first: int = 1) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "id": [first, first + 1],
            "dt": [pd.Timestamp("2026-04-01"), pd.Timestamp("2026-04-02")],
            "value": [f"value-{first}", f"value-{first + 1}"],
        }
    )


@pytest.mark.parametrize("backend", BACKENDS)
def test_read_execute_matrix(backend: str) -> None:
    if not _enabled(backend):
        pytest.skip("Greenplum requires x86_64")
    alias = _alias(backend)
    assert int(sql.read(alias, "SELECT 1 AS value").iloc[0, 0]) == 1
    assert int(sql.execute_read(alias, "SELECT 2 AS value").iloc[0, 0]) == 2
    assert sql.execute(alias, "SELECT 3", return_metadata=True).metadata.statement_count == 1


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("write_mode", WRITE_MODES)
def test_load_write_mode_matrix(backend: str, write_mode: str) -> None:
    if not _enabled(backend):
        pytest.skip("Greenplum requires x86_64")
    alias = _alias(backend, target=True)
    table = _table(backend, f"load_{write_mode}")
    options = _shape_options(backend)
    try:
        sql.load_df(alias, table, _frame(), write_mode="replace", **options)
        mode_options = {**options, **(_upsert_options(backend) if write_mode == "upsert" else {})}
        inserted = sql.load_df(alias, table, _frame(2), write_mode=write_mode, **mode_options)
        assert inserted == 2
        assert not sql.read(alias, f"SELECT * FROM {table}").empty
    finally:
        sql.drop_tables(
            alias,
            table,
            if_exists=True,
            ch_cluster="integration_cluster" if backend == "ch" else None,
        )


@pytest.mark.parametrize("source", BACKENDS)
@pytest.mark.parametrize("target", BACKENDS)
@pytest.mark.parametrize("write_mode", WRITE_MODES)
def test_transfer_pair_and_write_mode_matrix(
    source: str,
    target: str,
    write_mode: str,
) -> None:
    if not _enabled(source) or not _enabled(target):
        pytest.skip("Greenplum requires x86_64")
    source_alias = _alias(source)
    target_alias = _alias(target, target=True)
    source_table = _table(source, "transfer_source")
    target_table = _table(target, f"transfer_{write_mode}")
    try:
        sql.load_df(
            source_alias,
            source_table,
            _frame(),
            write_mode="replace",
            **_shape_options(source),
        )
        sql.load_df(
            target_alias,
            target_table,
            _frame(10),
            write_mode="replace",
            **_shape_options(target),
        )
        options = _shape_options(target)
        if write_mode == "upsert":
            options.update(_upsert_options(target))
        transferred = sql.transfer(
            source_alias,
            target_alias,
            from_table=source_table,
            to_table=target_table,
            write_mode=write_mode,
            batch_size=1,
            adaptive_batch_size=False,
            target_rows_per_second=False,
            **options,
        )
        assert transferred == 2
    finally:
        sql.drop_tables(
            source_alias,
            source_table,
            if_exists=True,
            ch_cluster="integration_cluster" if source == "ch" else None,
        )
        sql.drop_tables(
            target_alias,
            target_table,
            if_exists=True,
            ch_cluster="integration_cluster" if target == "ch" else None,
        )


@pytest.mark.parametrize("backend", BACKENDS)
def test_table_lifecycle_matrix(backend: str) -> None:
    if not _enabled(backend):
        pytest.skip("Greenplum requires x86_64")
    alias = _alias(backend, target=True)
    table = _table(backend, "lifecycle_matrix")
    try:
        sql.create_sql_table(alias, table, df=_frame(), **_shape_options(backend))
        info = sql.table_info(alias, table, include_row_count=True)
        assert info.exists
        assert info.resolved_table
        sql.cleanup_stale_stage_tables(alias, target_table=table)
    finally:
        sql.drop_tables(
            alias,
            table,
            if_exists=True,
            ch_cluster="integration_cluster" if backend == "ch" else None,
        )


@pytest.mark.parametrize("backend", BACKENDS)
def test_query_observability_matrix(backend: str) -> None:
    if not _enabled(backend):
        pytest.skip("Greenplum requires x86_64")
    result = sql.show_queries(_alias(backend), state="active")
    assert {"query_id", "state"}.issubset(result.columns)


def test_greenplum_partition_and_vacuum() -> None:
    if not _enabled("gp"):
        pytest.skip("Greenplum requires x86_64")
    table = _table("gp", "vacuum")
    try:
        sql.load_df("gp", table, _frame(), write_mode="replace", gp_distributed_by_key="id")
        sql.gp_vacuum(table, analyze=True, verbose=False, db_key="gp")
        plan = sql.gp_create_partitions(
            "gp",
            table,
            values=["2026-04-01"],
            dry_run=True,
        )
        assert plan.operation == "gp_create_partitions"
    finally:
        sql.drop_tables("gp", table, if_exists=True, ch_cluster=None)


def test_orchestration_task_matrix() -> None:
    tasks = [
        {"name": "trino", "type": "read", "db_key": "trino", "query": "SELECT 1"},
        {"name": "clickhouse", "type": "read", "db_key": "ch", "query": "SELECT 2"},
    ]
    parallel = sql.parallel_sql(tasks, concurrency=2, start_comment="/* matrix */")
    asynchronous = sql.async_sql(tasks, concurrency=2)
    assert list(parallel) == ["trino", "clickhouse"]
    assert list(asynchronous) == ["trino", "clickhouse"]
