from __future__ import annotations

import os
import uuid

import pandas as pd
import pytest
from analytics_toolkit import sql
from tests.integration.manifest import scenario_param
from tests.integration.support.identity import resource_name
from tests.integration.support.normalization import assert_exact_frame

pytestmark = [pytest.mark.integration, pytest.mark.integration_core]
BACKENDS = ("gp", "trino", "ch")
WRITE_MODES = ("append", "replace", "truncate_insert", "upsert")


def _enabled(backend: str) -> bool:
    return backend != "gp" or os.environ.get("SQL_INTEGRATION_GP") == "1"


def _alias(backend: str, *, target: bool = False) -> str:
    if backend == "gp":
        return "gp_target" if target else "gp_source"
    if backend == "trino":
        return "trino_target_values" if target else "trino_source_parquet"
    return "ch_target" if target else "ch_source"


def _table(backend: str, label: str) -> str:
    run_id = os.environ.get("SQL_INTEGRATION_RUN_ID", uuid.uuid4().hex[:8])
    test_id = os.environ.get("SQL_INTEGRATION_TEST_ID", "manual")
    suffix = resource_name(run_id, test_id, label)
    if backend == "gp":
        return f"public.{suffix}"
    if backend == "trino":
        return f"iceberg.integration.{suffix}"
    return f"integration.{suffix}"


def _shape_options(
    backend: str,
    *,
    ch_only_shard: bool = False,
) -> dict[str, object]:
    if backend == "gp":
        return {"gp_distributed_by_key": "id"}
    if backend == "trino":
        return {"partition_by": ["dt"]}
    return {
        "partition_by": ["dt"],
        "order_by": ["id"],
        "ch_engine": "MergeTree",
        "ch_shard_on_cluster": "integration_cluster",
        "ch_distributed_on_cluster": "integration_cluster",
        "ch_distributed_cluster": "integration_cluster",
        "ch_only_shard": ch_only_shard,
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
            "dt": [
                pd.Timestamp("2026-04-01").date(),
                pd.Timestamp("2026-04-02").date(),
            ],
            "value": [f"value-{first}", f"value-{first + 1}"],
        }
    )


def _transfer_seed_and_expected(write_mode: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    source = _frame()
    if write_mode == "upsert":
        seed = pd.DataFrame(
            {
                "id": [1, 10],
                "dt": [
                    pd.Timestamp("2026-04-01").date(),
                    pd.Timestamp("2026-05-01").date(),
                ],
                "value": ["stale-value", "unaffected-value"],
            }
        )
        expected = pd.concat([source, seed.iloc[[1]]], ignore_index=True)
    else:
        seed = _frame(10)
        expected = (
            pd.concat([source, seed], ignore_index=True) if write_mode == "append" else source
        )
    return seed, expected.sort_values("id").reset_index(drop=True)


@pytest.mark.parametrize(
    "backend",
    [scenario_param(f"query.read.{backend}", backend) for backend in BACKENDS],
)
def test_read_execute_matrix(backend: str) -> None:
    if not _enabled(backend):
        pytest.skip("Greenplum requires x86_64")
    alias = _alias(backend)
    assert int(sql.read(alias, "SELECT 1 AS value").iloc[0, 0]) == 1
    assert int(sql.execute_read(alias, "SELECT 2 AS value").iloc[0, 0]) == 2
    assert sql.execute(alias, "SELECT 3", return_metadata=True).metadata.statement_count == 1


@pytest.mark.parametrize(
    ("backend", "write_mode"),
    [
        scenario_param(f"load.{backend}.{write_mode}", backend, write_mode)
        for backend in BACKENDS
        for write_mode in WRITE_MODES
    ],
)
def test_load_write_mode_matrix(backend: str, write_mode: str) -> None:
    if not _enabled(backend):
        pytest.skip("Greenplum requires x86_64")
    alias = _alias(backend, target=True)
    table = _table(backend, f"load_{write_mode}")
    options = _shape_options(
        backend,
        ch_only_shard=backend == "ch" and write_mode == "upsert",
    )
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


@pytest.mark.parametrize(
    ("source", "target", "write_mode"),
    [
        scenario_param(
            f"transfer.{source}.{target}.{write_mode}",
            source,
            target,
            write_mode,
        )
        for source in BACKENDS
        for target in BACKENDS
        for write_mode in WRITE_MODES
    ],
)
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
    seed, expected = _transfer_seed_and_expected(write_mode)
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
            seed,
            write_mode="replace",
            **_shape_options(
                target,
                ch_only_shard=target == "ch" and write_mode == "upsert",
            ),
        )
        options = _shape_options(
            target,
            ch_only_shard=target == "ch" and write_mode == "upsert",
        )
        if write_mode == "upsert":
            options.update(_upsert_options(target))
        if target == "ch":
            options["table_schema"] = {
                "id": "Int64",
                "dt": "Date",
                "value": "String",
            }
        transferred = sql.transfer(
            source_alias,
            target_alias,
            from_table=source_table,
            to_table=target_table,
            write_mode=write_mode,
            batch_size=1,
            adaptive_batch_size=False,
            target_rows_per_second=False,
            retry_cnt=1,
            timeout_increment=0,
            full_retry_cnt=1,
            full_timeout_increment=0,
            **options,
        )
        assert transferred == 2
        actual = sql.read(
            target_alias,
            f"SELECT id, dt, value FROM {target_table} ORDER BY id",
        )
        assert_exact_frame(actual, expected, date_columns=("dt",))
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


@pytest.mark.parametrize(
    "backend",
    [scenario_param(f"lifecycle.{backend}", backend) for backend in BACKENDS],
)
def test_table_lifecycle_matrix(backend: str) -> None:
    if not _enabled(backend):
        pytest.skip("Greenplum requires x86_64")
    alias = _alias(backend, target=True)
    table = _table(backend, "lifecycle_matrix")
    try:
        sql.create_sql_table(alias, table, df=_frame(), **_shape_options(backend))
        info = sql.table_info(alias, table, include_row_count=True)
        assert info.exists
        if backend == "trino":
            assert info.resolved_table == table
        if backend == "ch":
            assert info.shard_table == f"{table}_shard"
        sql.cleanup_stale_stage_tables(alias, target_table=table)
    finally:
        sql.drop_tables(
            alias,
            table,
            if_exists=True,
            ch_cluster="integration_cluster" if backend == "ch" else None,
        )


@pytest.mark.parametrize(
    "backend",
    [scenario_param(f"observability.{backend}", backend) for backend in BACKENDS],
)
def test_query_observability_matrix(backend: str) -> None:
    if not _enabled(backend):
        pytest.skip("Greenplum requires x86_64")
    result = sql.show_queries(_alias(backend), state="active")
    assert {"query_id", "state"}.issubset(result.columns)


@pytest.mark.sql_scenario("maintenance.gp")
def test_greenplum_partition_and_vacuum() -> None:
    if not _enabled("gp"):
        pytest.skip("Greenplum requires x86_64")
    table = _table("gp", "vacuum")
    try:
        sql.load_df("gp", table, _frame(), write_mode="replace", gp_distributed_by_key="id")
        sql.gp_vacuum("gp", table, analyze=True, verbose=False)
        plan = sql.gp_create_partitions(
            "gp",
            table,
            values=["2026-04-01"],
            dry_run=True,
        )
        assert plan.operation == "gp_create_partitions"
    finally:
        sql.drop_tables("gp", table, if_exists=True, ch_cluster=None)


@pytest.mark.sql_scenario("orchestration.parallel_async")
def test_orchestration_task_matrix() -> None:
    tasks = [
        {"name": "trino", "type": "read", "db_key": "trino", "query": "SELECT 1"},
        {"name": "clickhouse", "type": "read", "db_key": "ch", "query": "SELECT 2"},
    ]
    parallel = sql.parallel_sql(tasks, concurrency=2, start_comment="/* matrix */")
    asynchronous = sql.async_sql(tasks, concurrency=2)
    assert list(parallel) == ["trino", "clickhouse"]
    assert list(asynchronous) == ["trino", "clickhouse"]
