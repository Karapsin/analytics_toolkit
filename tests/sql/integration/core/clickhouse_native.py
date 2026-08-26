from __future__ import annotations

import os
import uuid

import pandas as pd
import pytest
from analytics_toolkit import sql

from tests.sql.integration._support.identity import resource_name

pytestmark = [pytest.mark.integration, pytest.mark.integration_core]


def _table(suffix: str) -> str:
    run_id = os.environ.get("SQL_INTEGRATION_RUN_ID", uuid.uuid4().hex[:8])
    test_id = os.environ.get("SQL_INTEGRATION_TEST_ID", "manual")
    return f"integration.{resource_name(run_id, test_id, suffix)}"


@pytest.mark.sql_scenario("clickhouse.native.transport")
def test_native_clickhouse_public_operations_and_transfers() -> None:
    native_table = _table("native")
    stream_source = _table("native_stream")
    http_target = _table("native_http_target")
    native_target = _table("native_target")
    distributed = _table("native_distributed")
    frame = pd.DataFrame(
        {
            "id": [1, 2, 3],
            "dt": pd.to_datetime(["2026-01-01", "2026-01-02", "2026-01-02"]),
            "value": ["one", "two", "three"],
        }
    )
    try:
        version = sql.read("ch_native", "SELECT version() AS version")
        assert version.iloc[0, 0]
        sql.execute("ch_native", "SELECT 1")

        assert (
            sql.load_df(
                "ch_native",
                native_table,
                frame,
                write_mode="replace",
                partition_by="dt",
                order_by=["dt", "id"],
            )
            == 3
        )
        assert sql.read("ch_native", f"SELECT count() FROM {native_table}").iloc[0, 0] == 3
        sql.drop_partitions("ch_native", native_table, ["2026-01-01"])
        assert sql.read("ch_native", f"SELECT count() FROM {native_table}").iloc[0, 0] == 2

        async_result = sql.async_sql(
            [
                {"name": "ok", "type": "read", "db_key": "ch_native", "query": "SELECT 7"},
                {
                    "name": "bad",
                    "type": "read",
                    "db_key": "ch_native",
                    "query": "SELECT missing_native_column",
                    "retry_cnt": 1,
                },
            ],
            concurrency=2,
            fail_fast=False,
        )
        assert int(async_result["ok"].iloc[0, 0]) == 7
        assert isinstance(async_result["bad"], str)

        sql.execute(
            "ch_native",
            f"CREATE TABLE {stream_source} (id UInt64) ENGINE = MergeTree ORDER BY id",
        )
        sql.execute(
            "ch_native",
            f"INSERT INTO {stream_source} SELECT number FROM numbers(70000)",
        )
        assert (
            sql.transfer(
                "ch_native",
                "ch",
                from_table=stream_source,
                to_table=http_target,
                write_mode="replace",
                batch_size=70_000,
                adaptive_batch_size=False,
                target_rows_per_second=False,
                retry_cnt=1,
                full_retry_cnt=1,
                table_schema={"id": "UInt64"},
                order_by="id",
                ch_only_shard=True,
            )
            == 70_000
        )
        assert (
            sql.transfer(
                "ch",
                "ch_native",
                from_table=http_target,
                to_table=native_target,
                write_mode="replace",
                batch_size=65_536,
                adaptive_batch_size=False,
                target_rows_per_second=False,
                retry_cnt=1,
                full_retry_cnt=1,
                table_schema={"id": "UInt64"},
                order_by="id",
                ch_only_shard=True,
            )
            == 70_000
        )
        assert sql.read("ch_native", f"SELECT count() FROM {native_target}").iloc[0, 0] == 70_000

        sql.load_df(
            "ch_native",
            distributed,
            frame.iloc[:1],
            write_mode="replace",
            partition_by="dt",
            order_by="id",
        )
        assert sql.read("ch_native", f"SELECT count() FROM {distributed}").iloc[0, 0] == 1
    finally:
        for table in (distributed, native_target, http_target, stream_source, native_table):
            sql.drop_tables(
                "ch_native",
                table,
                if_exists=True,
                ch_cluster="integration_cluster",
            )
