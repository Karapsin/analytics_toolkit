from __future__ import annotations

# ruff: noqa: I001, PT018, TC002

from decimal import Decimal
import os
import uuid

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
from tests.integration.support.identity import query_label
from tests.integration.support.resources import ResourceRegistry

pytestmark = [pytest.mark.integration, pytest.mark.integration_core]


def _query_label(purpose: str) -> str:
    return query_label(
        os.environ.get("SQL_INTEGRATION_RUN_ID", uuid.uuid4().hex[:8]),
        os.environ.get("SQL_INTEGRATION_TEST_ID", "manual"),
        purpose,
    )


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


@pytest.mark.sql_scenario("schema.copy.trino.complex")
def test_create_sql_table_preserves_same_trino_array_types(
    resource_registry: ResourceRegistry,
) -> None:
    alias = "trino_target_values"
    target_table = _registered(
        resource_registry,
        "trino",
        alias,
        "schema_complex_target",
    )
    source_query = """
        SELECT
            CAST(ARRAY['campaign-a', 'campaign-b'] AS ARRAY(VARCHAR))
                AS campaign_codes,
            CAST(ARRAY[from_hex('01'), from_hex('ff')] AS ARRAY(VARBINARY))
                AS po_bonus_pk,
            CAST(ARRAY[from_hex('0a')] AS ARRAY(VARBINARY))
                AS pers_offers_pk
    """

    sql.create_sql_table(
        alias,
        target_table,
        sql=source_query,
        insert_data=False,
        retry_cnt=1,
        query_label=_query_label("trino_complex_schema_empty"),
    )
    empty_info = sql.table_info(alias, target_table, include_row_count=True)
    assert empty_info.exists and empty_info.row_count == 0
    assert {name: type_name.lower() for name, type_name in empty_info.columns.items()} == {
        "campaign_codes": "array(varchar)",
        "po_bonus_pk": "array(varbinary)",
        "pers_offers_pk": "array(varbinary)",
    }

    inserted = sql.create_sql_table(
        alias,
        target_table,
        sql=source_query,
        insert_data=True,
        drop_target_if_exists=True,
        retry_cnt=1,
        query_label=_query_label("trino_complex_schema_insert"),
    )

    assert inserted == 1
    actual = sql.read(alias, f"SELECT * FROM {target_table}")
    assert actual.to_dict(orient="records") == [
        {
            "campaign_codes": ["campaign-a", "campaign-b"],
            "po_bonus_pk": [b"\x01", b"\xff"],
            "pers_offers_pk": [b"\x0a"],
        }
    ]


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


@pytest.mark.sql_scenario("ddl.reconfigure.ch")
def test_clickhouse_reconfigure_managed_pair(resource_registry: ResourceRegistry) -> None:
    table = _registered(
        resource_registry,
        "ch",
        "ch_target",
        "ddl_ch_reconfigure",
        distributed=True,
    )
    frame = _portable_frame()
    sql.create_sql_table(
        "ch_target",
        table,
        df=frame,
        partition_by=["event_date"],
        order_by=["row_id"],
        ch_engine="MergeTree",
        ch_cluster="integration_cluster",
        ch_distributed_table=True,
        retry_cnt=1,
    )
    sql.load_df(
        "ch_target",
        table,
        frame,
        write_mode="append",
        retry_cnt=1,
    )

    plan = sql.ch_reconfigure_table(
        "ch_target",
        table,
        ch_partition_by="toYYYYMM(event_date)",
        ch_order_by=["event_date", "row_id"],
        ch_cluster="integration_cluster",
        retry_cnt=1,
        dry_run=True,
    )
    assert isinstance(plan, sql.SqlPlan)
    assert plan.operation == "ch_reconfigure_table"
    assert any(statement.phase == "cutover" for statement in plan.statements)

    result = sql.ch_reconfigure_table(
        "ch_target",
        table,
        ch_partition_by="toYYYYMM(event_date)",
        ch_order_by=["event_date", "row_id"],
        ch_cluster="integration_cluster",
        retry_cnt=1,
        return_metadata=True,
    )
    assert isinstance(result, sql.SqlOperationResult)
    assert result.data["strategy"] == "managed_pair_rebuild"
    assert result.data["row_count_validated"] is True
    assert result.data["cleanup_complete"] is True
    assert len(sql.read("ch_target", f"SELECT * FROM {table}")) == 2
    ddl = " ".join(sql.extract_ddl("ch_target", f"{table}_shard").upper().split())
    assert "TOYYYYMM(EVENT_DATE)" in ddl
    assert "ORDER BY (EVENT_DATE, ROW_ID)" in ddl


@pytest.mark.parametrize(
    "backend",
    [scenario_param(f"ddl.options.{backend}", backend) for backend in BACKENDS],
)
def test_create_table_generation_metadata_and_invalid_schema_options(
    backend: str,
    resource_registry: ResourceRegistry,
) -> None:
    if not backend_enabled(backend):
        pytest.skip("Greenplum requires x86_64")
    alias = backend_alias(backend, target=True)
    dry_table = resource_registry.table(alias, integration_table(backend, "ddl_dry_run"))
    options = _copy_options(backend)
    plan = sql.create_sql_table(
        alias,
        dry_table,
        table_schema=_schema(backend),
        dry_run=True,
        query_label=_query_label("ddl_options"),
        **options,
    )
    assert plan.statements
    assert not sql.table_info(alias, dry_table).exists

    generated = sql.create_sql_table(
        alias,
        dry_table,
        table_schema=_schema(backend),
        only_generate_sql=True,
        **options,
    )
    assert isinstance(generated, str) and "CREATE" in generated.upper()

    result = sql.create_sql_table(
        alias,
        dry_table,
        table_schema=_schema(backend),
        return_metadata=True,
        retry_cnt=1,
        **options,
    )
    assert result.metadata.statement_count >= 1
    assert sql.table_info(alias, dry_table).exists

    invalid_table = resource_registry.table(alias, integration_table(backend, "ddl_invalid"))
    with pytest.raises(Exception, match=r"(?i)type|schema|syntax|unknown"):
        sql.create_sql_table(
            alias,
            invalid_table,
            table_schema={"row_id": "DEFINITELY_NOT_A_REAL_TYPE"},
            retry_cnt=1,
            **options,
        )
    assert not sql.table_info(alias, invalid_table).exists

    missing_table = resource_registry.table(
        alias,
        integration_table(backend, "ddl_missing_source"),
    )
    with pytest.raises(Exception, match=r"(?i)not found|does not exist|unknown|missing"):
        sql.create_sql_table(
            alias,
            missing_table,
            sql="SELECT * FROM integration.definitely_missing_source_table",
            source_db=alias,
            insert_data=True,
            retry_cnt=1,
            **_copy_options(backend),
        )
    assert not sql.table_info(alias, missing_table).exists


@pytest.mark.sql_scenario("ddl.native.gp.distribution_variants")
def test_greenplum_random_and_single_key_distribution(
    resource_registry: ResourceRegistry,
) -> None:
    if not backend_enabled("gp"):
        pytest.skip("Greenplum requires x86_64")
    random_table = _registered(resource_registry, "gp", "gp_target", "ddl_gp_random")
    keyed_table = _registered(resource_registry, "gp", "gp_target", "ddl_gp_keyed")
    sql.create_sql_table("gp_target", random_table, table_schema=_schema("gp"), retry_cnt=1)
    sql.create_sql_table(
        "gp_target",
        keyed_table,
        table_schema=_schema("gp"),
        gp_distributed_by_key=["row_id"],
        retry_cnt=1,
    )
    random_ddl = " ".join(sql.extract_ddl("gp_target", random_table).upper().split())
    keyed_ddl = " ".join(sql.extract_ddl("gp_target", keyed_table).upper().split())
    assert "DISTRIBUTED RANDOMLY" in random_ddl
    assert "DISTRIBUTED BY" in keyed_ddl and "ROW_ID" in keyed_ddl


@pytest.mark.sql_scenario("ddl.native.gp.partition_range")
def test_greenplum_initial_range_partitions(
    resource_registry: ResourceRegistry,
) -> None:
    if not backend_enabled("gp"):
        pytest.skip("Greenplum requires x86_64")
    table = _registered(resource_registry, "gp", "gp_target", "ddl_gp_range")
    load_table = _registered(
        resource_registry,
        "gp",
        "gp_target",
        "ddl_gp_range_load",
    )
    source_table = _registered(
        resource_registry,
        "gp",
        "gp_source",
        "ddl_gp_range_source",
    )
    transfer_table = _registered(
        resource_registry,
        "gp",
        "gp_target",
        "ddl_gp_range_transfer",
    )
    partitions = {
        "start": "2026-01-01",
        "end": "2026-04-01",
        "interval": "1 month",
    }
    create_options = {
        "table_schema": {"event_date": "DATE", "row_id": "BIGINT"},
        "partition_by": "event_date",
        "gp_partitions": partitions,
        "gp_distributed_by_key": "row_id",
        "query_label": _query_label("gp_partition_range"),
        "retry_cnt": 1,
    }
    generated = sql.create_sql_table(
        "gp_target",
        table,
        only_generate_sql=True,
        **create_options,
    )
    assert "PARTITION BY RANGE" in generated
    assert "EVERY (INTERVAL '1 month')" in generated
    sql.create_sql_table("gp_target", table, **create_options)

    initial = pd.DataFrame(
        {
            "event_date": [pd.Timestamp("2026-01-15").date(), pd.Timestamp("2026-02-20").date()],
            "row_id": [1, 2],
        }
    )
    sql.load_df(
        "gp_target",
        table,
        initial,
        write_mode="append",
        partition_by="event_date",
        gp_partitions=partitions,
        query_label=_query_label("gp_partition_range_insert"),
        retry_cnt=1,
    )
    assert_exact_frame(
        sql.read("gp_target", f"SELECT * FROM {table} ORDER BY row_id"),
        initial,
        date_columns=("event_date",),
    )
    with pytest.raises(Exception, match=r"(?i)partition|range|SQL context"):
        sql.execute(
            "gp_target",
            f"INSERT INTO {table} VALUES ('2026-05-01', 99)",
            query_label=_query_label("gp_partition_range_reject"),
            retry_cnt=1,
        )
    assert len(sql.read("gp_target", f"SELECT * FROM {table}")) == 2

    extracted = " ".join(sql.extract_ddl("gp_target", table).upper().split())
    assert "PARTITION BY RANGE" in extracted
    assert "2026-01-01" in extracted and "2026-04-01" in extracted
    sql.gp_create_partitions(
        "gp_target",
        table,
        months=["2026-04-01"],
        query_label=_query_label("gp_partition_range_extend"),
        retry_cnt=1,
    )
    sql.execute(
        "gp_target",
        f"INSERT INTO {table} VALUES ('2026-04-15', 3)",
        query_label=_query_label("gp_partition_range_extended_insert"),
        retry_cnt=1,
    )
    assert len(sql.read("gp_target", f"SELECT * FROM {table}")) == 3

    sql.load_df(
        "gp_target",
        load_table,
        initial,
        write_mode="replace",
        partition_by="event_date",
        gp_partitions=partitions,
        query_label=_query_label("gp_partition_range_load_create"),
        retry_cnt=1,
    )
    assert len(sql.read("gp_target", f"SELECT * FROM {load_table}")) == 2

    sql.load_df(
        "gp_source",
        source_table,
        initial,
        write_mode="replace",
        retry_cnt=1,
    )
    sql.transfer(
        "gp_source",
        "gp_target",
        from_table=source_table,
        to_table=transfer_table,
        write_mode="replace",
        table_schema={"event_date": "DATE", "row_id": "BIGINT"},
        partition_by="event_date",
        gp_partitions=partitions,
        query_label=_query_label("gp_partition_range_transfer_create"),
        retry_cnt=1,
        full_retry_cnt=1,
    )
    assert len(sql.read("gp_target", f"SELECT * FROM {transfer_table}")) == 2


@pytest.mark.sql_scenario("ddl.native.gp.partition_list")
def test_greenplum_initial_list_partitions(
    resource_registry: ResourceRegistry,
) -> None:
    if not backend_enabled("gp"):
        pytest.skip("Greenplum requires x86_64")
    table = _registered(resource_registry, "gp", "gp_target", "ddl_gp_list")
    partitions = {"values": ["free", "paid"]}
    frame = pd.DataFrame({"segment": ["free", "paid"], "row_id": [1, 2]})
    sql.create_sql_table(
        "gp_target",
        table,
        table_schema={"segment": "TEXT", "row_id": "BIGINT"},
        partition_by="segment",
        gp_partitions=partitions,
        query_label=_query_label("gp_partition_list"),
        retry_cnt=1,
    )
    sql.load_df(
        "gp_target",
        table,
        frame,
        write_mode="append",
        partition_by="segment",
        gp_partitions=partitions,
        query_label=_query_label("gp_partition_list_insert"),
        retry_cnt=1,
    )
    assert_exact_frame(
        sql.read("gp_target", f"SELECT * FROM {table} ORDER BY row_id"),
        frame,
    )
    extracted = " ".join(sql.extract_ddl("gp_target", table).upper().split())
    assert "PARTITION BY LIST" in extracted
    assert "P_FREE" in extracted and "P_PAID" in extracted
    with pytest.raises(Exception, match=r"(?i)partition|SQL context"):
        sql.execute(
            "gp_target",
            f"INSERT INTO {table} VALUES ('enterprise', 99)",
            query_label=_query_label("gp_partition_list_reject"),
            retry_cnt=1,
        )
    assert len(sql.read("gp_target", f"SELECT * FROM {table}")) == 2


@pytest.mark.sql_scenario("ddl.native.ch.shard_replace")
def test_clickhouse_shard_only_replace_flow(resource_registry: ResourceRegistry) -> None:
    table = _registered(resource_registry, "ch", "ch_target", "ddl_ch_replace")
    options = {
        "table_schema": _schema("ch"),
        "ch_engine": "MergeTree",
        "order_by": ["row_id"],
        "ch_only_shard": True,
        "ch_replace_table": True,
        "retry_cnt": 1,
    }
    sql.create_sql_table("ch_target", table, **options)
    sql.create_sql_table("ch_target", table, **options)
    ddl = " ".join(sql.extract_ddl("ch_target", table).upper().split())
    assert "MERGETREE" in ddl and "ORDER BY" in ddl
    info = sql.table_info("ch_target", table)
    assert info.exists
    assert info.shard_table == f"{table}_shard"
