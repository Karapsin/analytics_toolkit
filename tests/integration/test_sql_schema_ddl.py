from __future__ import annotations

# ruff: noqa: I001, PT018, TC002

from decimal import Decimal

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
from tests.integration.support.resources import ResourceRegistry

pytestmark = [pytest.mark.integration, pytest.mark.integration_core]


def _portable_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "row_id": [1, 2],
            "event_date": [pd.Timestamp("2026-03-01").date(), pd.Timestamp("2026-03-02").date()],
            "value": ["Копия", "copy 😀"],
            "amount": [Decimal("1.2500"), Decimal("-2.5000")],
        }
    )


def _schema(backend: str) -> dict[str, str]:
    if backend == "gp":
        return {
            "row_id": "BIGINT",
            "event_date": "DATE",
            "value": "TEXT",
            "amount": "NUMERIC(18,4)",
        }
    if backend == "ch":
        return {
            "row_id": "Int64",
            "event_date": "Date",
            "value": "String",
            "amount": "Decimal(18,4)",
        }
    return {
        "row_id": "BIGINT",
        "event_date": "DATE",
        "value": "VARCHAR",
        "amount": "DECIMAL(18,4)",
    }


def _registered(
    registry: ResourceRegistry,
    backend: str,
    alias: str,
    purpose: str,
    *,
    distributed: bool = False,
) -> str:
    return registry.table(
        alias,
        integration_table(backend, purpose),
        ch_cluster="integration_cluster" if backend == "ch" and distributed else None,
    )


def _copy_options(backend: str) -> dict[str, object]:
    if backend == "ch":
        return {
            "order_by": "tuple()",
            "ch_engine": "MergeTree",
            "ch_cluster": "integration_cluster",
            "ch_only_shard": True,
        }
    return table_options(backend)


@pytest.mark.parametrize(
    ("source", "target"),
    [
        scenario_param(f"schema.copy.{source}.{target}", source, target)
        for source in BACKENDS
        for target in BACKENDS
    ],
)
def test_create_sql_table_schema_copy_matrix(
    source: str,
    target: str,
    resource_registry: ResourceRegistry,
) -> None:
    if not backend_enabled(source) or not backend_enabled(target):
        pytest.skip("Greenplum requires x86_64")
    source_alias = backend_alias(source)
    target_alias = backend_alias(target, target=True)
    source_table = _registered(resource_registry, source, source_alias, "schema_source")
    target_table = _registered(resource_registry, target, target_alias, "schema_target")
    frame = _portable_frame()
    sql.load_df(
        source_alias,
        source_table,
        frame,
        write_mode="replace",
        table_schema=_schema(source),
        **table_options(source, only_shard=source == "ch"),
    )

    query = f"SELECT row_id, event_date, value, amount FROM {source_table}"
    sql.create_sql_table(
        target_alias,
        target_table,
        sql=query,
        source_db=source_alias,
        insert_data=False,
        retry_cnt=1,
        **_copy_options(target),
    )
    empty_info = sql.table_info(target_alias, target_table, include_row_count=True)
    assert empty_info.exists and empty_info.row_count == 0

    inserted = sql.create_sql_table(
        target_alias,
        target_table,
        sql=query,
        source_db=source_alias,
        insert_data=True,
        drop_target_if_exists=True,
        retry_cnt=1,
        **_copy_options(target),
    )
    assert inserted == len(frame)
    actual = sql.read(target_alias, f"SELECT * FROM {target_table} ORDER BY row_id")
    assert_exact_frame(
        actual,
        frame,
        decimal_columns=("amount",),
        date_columns=("event_date",),
    )
    assert list(sql.table_info(target_alias, target_table).columns) == list(frame.columns)


@pytest.mark.sql_scenario("ddl.native.gp")
def test_greenplum_native_distribution_ddl(resource_registry: ResourceRegistry) -> None:
    if not backend_enabled("gp"):
        pytest.skip("Greenplum requires x86_64")
    table = _registered(resource_registry, "gp", "gp_target", "ddl_gp")
    sql.create_sql_table(
        "gp_target",
        table,
        table_schema=_schema("gp"),
        gp_distributed_by_key=["row_id", "event_date"],
        retry_cnt=1,
    )
    ddl = " ".join(sql.extract_ddl("gp_target", table).upper().split())
    assert "DISTRIBUTED BY" in ddl
    assert "ROW_ID" in ddl and "EVENT_DATE" in ddl


@pytest.mark.sql_scenario("ddl.native.trino")
def test_trino_native_iceberg_properties(resource_registry: ResourceRegistry) -> None:
    table = _registered(resource_registry, "trino", "trino_target_values", "ddl_trino")
    sql.create_sql_table(
        "trino_target_values",
        table,
        table_schema=_schema("trino"),
        partition_by=["event_date"],
        retry_cnt=1,
    )
    ddl = " ".join(sql.extract_ddl("trino_target_values", table).lower().split())
    assert "with" in ddl
    assert "partitioning" in ddl and "event_date" in ddl


@pytest.mark.sql_scenario("ddl.native.ch")
def test_clickhouse_native_distributed_ddl(resource_registry: ResourceRegistry) -> None:
    table = _registered(
        resource_registry,
        "ch",
        "ch_target",
        "ddl_ch",
        distributed=True,
    )
    create_options = {
        "table_schema": _schema("ch"),
        "partition_by": ["event_date"],
        "order_by": ["row_id"],
        "ch_engine": "MergeTree",
        "ch_cluster": "integration_cluster",
        "ch_distributed_table": True,
        "ch_sharding_key": "row_id",
        "retry_cnt": 1,
    }
    generated = sql.create_sql_table(
        "ch_target",
        table,
        only_generate_sql=True,
        **create_options,
    )
    assert isinstance(generated, str) and "ON CLUSTER integration_cluster" in generated
    sql.create_sql_table(
        "ch_target",
        table,
        **create_options,
    )
    ddl = " ".join(sql.extract_ddl("ch_target", [table, f"{table}_shard"]).upper().split())
    assert "ENGINE = DISTRIBUTED" in ddl or "ENGINE= DISTRIBUTED" in ddl
    assert "MERGETREE" in ddl
    assert "ORDER BY" in ddl and "PARTITION BY" in ddl
    assert ddl.index(table.upper()) < ddl.index(
        f"{table}_SHARD".upper(), ddl.index(table.upper()) + 1
    )
    info = sql.table_info("ch_target", table)
    assert info.shard_table == f"{table}_shard"
