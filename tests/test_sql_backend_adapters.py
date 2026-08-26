from __future__ import annotations

import importlib
import inspect
import threading
from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pandas as pd
import pytest
from analytics_toolkit.sql.backends import (
    BACKEND_ADAPTERS,
    BACKEND_REGISTRY,
    get_backend,
    get_backend_adapter,
    get_backend_names,
)
from analytics_toolkit.sql.backends.base import BackendAdapter
from analytics_toolkit.sql.backends.gp.adapter import GP_IDENTIFIER_MAX_BYTES
from analytics_toolkit.sql.backends.models import (
    SourceColumn,
    StageFinalizationRequest,
    StageTargetTableRequest,
    TargetWriteModeRequest,
)
from analytics_toolkit.sql.backends.registry import (
    backend_capability_map,
    get_backend_capability,
    normalize_backend_name,
    require_backend_name,
    supported_backend_message,
)
from analytics_toolkit.sql.connection.errors import (
    InvalidSqlInputError,
    SqlConfigError,
    UnsupportedConnectionTypeError,
)
from tests.sql_fakes import FakeClickHouseResult, FakeDbapiConnection

sql_module = importlib.import_module("analytics_toolkit.sql")
read_sql_module = importlib.import_module("analytics_toolkit.sql.dml.io.read_sql")
execute_sql_module = importlib.import_module("analytics_toolkit.sql.dml.io.execute_sql")
execute_read_module = importlib.import_module("analytics_toolkit.sql.dml.io.execute_read")
load_sql_table_module = importlib.import_module("analytics_toolkit.sql.dml.load.load_sql_table")
table_ops_module = importlib.import_module("analytics_toolkit.sql.dml.table.api")
table_basic_ops_module = importlib.import_module("analytics_toolkit.sql.dml.table._basic_ops")
ch_lifecycle_module = importlib.import_module("analytics_toolkit.sql.backends.ch.lifecycle")
ch_backend_wait_module = importlib.import_module("analytics_toolkit.sql.backends.ch.wait")
backend_registry_module = importlib.import_module("analytics_toolkit.sql.backends.registry")
backend_validation_module = importlib.import_module("analytics_toolkit.sql.backends.validation")
backend_source_count_module = importlib.import_module("analytics_toolkit.sql.backends.source_count")
backend_common_methods_module = importlib.import_module(
    "analytics_toolkit.sql.backends.common_methods"
)
gp_stage_module = importlib.import_module("analytics_toolkit.sql.backends.gp.stage")
adapter_defaults_module = importlib.import_module("analytics_toolkit.sql.backends.adapter_defaults")
trino_adapter_module = importlib.import_module("analytics_toolkit.sql.backends.trino.adapter")
ch_adapter_module = importlib.import_module("analytics_toolkit.sql.backends.ch.adapter")
ch_lifecycle_backend_module = ch_lifecycle_module
ch_ddl_backend_module = importlib.import_module("analytics_toolkit.sql.backends.ch.ddl")
ch_insert_backend_module = importlib.import_module("analytics_toolkit.sql.backends.ch.insert")
ch_operations_backend_module = importlib.import_module(
    "analytics_toolkit.sql.backends.ch.operations"
)
ch_queries_backend_module = importlib.import_module("analytics_toolkit.sql.backends.ch.queries")
ch_target_create_backend_module = importlib.import_module(
    "analytics_toolkit.sql.backends.ch.target_create"
)
ch_upsert_backend_module = importlib.import_module("analytics_toolkit.sql.backends.ch.upsert")


class RecordingClickHouseClient:
    def __init__(self) -> None:
        self.commands: list[tuple[str, dict[str, object] | None]] = []
        self.queries: list[str] = []

    def command(
        self,
        sql: str,
        settings: dict[str, object] | None = None,
    ) -> dict[str, int] | None:
        self.commands.append((sql, settings))
        if sql.startswith("INSERT INTO "):
            return {"written_rows": 3}
        return None

    def query(self, sql: str) -> FakeClickHouseResult:
        self.queries.append(sql)
        if sql.startswith("EXISTS TABLE "):
            return FakeClickHouseResult([(1,)])
        if sql.startswith("SELECT count()"):
            return FakeClickHouseResult([(9,)])
        if sql.startswith("DESCRIBE TABLE "):
            return FakeClickHouseResult([("id", "Nullable(Int64)")])
        return FakeClickHouseResult([])


def test_sql_public_api_exports_are_stable() -> None:
    public_names = {
        "async_sql",
        "ch_reconfigure_table",
        "create_sql_table",
        "drop_partitions",
        "drop_tables",
        "execute",
        "execute_read",
        "load_df",
        "parallel_sql",
        "read",
        "table_info",
        "transfer",
    }

    for name in public_names:
        assert name in sql_module.__all__
        assert callable(getattr(sql_module, name))

    assert list(inspect.signature(sql_module.load_df).parameters)[:3] == [
        "db_key",
        "destination_table",
        "df",
    ]
    assert list(inspect.signature(sql_module.transfer).parameters)[:4] == [
        "from_db",
        "to_db",
        "from_sql",
        "to_table",
    ]
    assert "format_plan" in sql_module.__all__
    assert "SqlTableInfo" in sql_module.__all__
    assert "ch_create_table_as" not in sql_module.__all__
    assert not hasattr(sql_module, "ch_create_table_as")
    assert "ch_full_table_move" not in sql_module.__all__
    assert not hasattr(sql_module, "ch_full_table_move")
    assert "create_table_from_sql" not in sql_module.__all__
    assert not hasattr(sql_module, "create_table_from_sql")
    assert "execute_sql" not in sql_module.__all__
    assert "read_sql" not in sql_module.__all__
    assert "drop_table" not in sql_module.__all__
    assert not hasattr(sql_module, "drop_table")
    assert "drop_paritions" not in sql_module.__all__
    assert not hasattr(sql_module, "drop_paritions")
    assert "drop_many_partitions" not in sql_module.__all__
    assert not hasattr(sql_module, "drop_many_partitions")
    assert "transfer_table" not in sql_module.__all__


def test_sql_public_api_functions_are_timed() -> None:
    for name in sql_module._TIMED_PUBLIC_SQL_FUNCTION_NAMES:
        assert getattr(getattr(sql_module, name), "__sql_public_timing__", False)

    assert callable(sql_module.execute)
    assert callable(sql_module.read)
    assert callable(sql_module.transfer)
    assert not hasattr(sql_module, "execute_sql")
    assert not hasattr(sql_module, "read_sql")
    assert not hasattr(sql_module, "transfer_table")


def test_table_ops_compatibility_helpers_remain_importable() -> None:
    helper_names = {
        "build_analyze_table_sql",
        "build_clear_table_sqls",
        "build_count_table_rows_sql",
        "build_drop_ch_distributed_table_pair_sqls",
        "build_drop_table_sql",
        "build_insert_from_query_sql",
        "build_insert_from_table_sql",
        "clear_target_table",
        "count_table_rows",
        "drop_table",
        "finalize_stage_table",
        "get_table_column_types",
        "get_trino_table_column_types",
        "insert_from_query",
        "insert_from_table",
        "table_exists",
        "_build_typed_insert_select_sql",
        "_ch_cluster_clause",
        "_execute_ch_command",
        "_gp_table_exists",
        "_trino_table_exists",
    }

    for name in helper_names:
        assert callable(getattr(table_ops_module, name))


def test_table_ops_reexports_split_basic_helpers() -> None:
    helper_names = {
        "build_analyze_table_sql",
        "build_clear_table_sqls",
        "build_count_table_rows_sql",
        "build_drop_ch_distributed_table_pair_sqls",
        "build_drop_table_sql",
        "build_insert_from_query_sql",
        "build_insert_from_table_sql",
        "count_table_rows",
        "get_table_column_types",
        "get_trino_table_column_types",
        "insert_from_query",
        "insert_from_table",
        "quote_qualified_table_name",
        "split_trino_table_name",
        "table_exists",
        "_build_typed_insert_select_sql",
        "_ch_cluster_clause",
        "_execute_ch_command",
        "_gp_table_exists",
        "_trino_table_exists",
    }

    for name in helper_names:
        assert getattr(table_ops_module, name) is getattr(table_basic_ops_module, name)


def test_backend_adapter_registry_renders_existing_sql_shapes() -> None:
    expected_backends = set(get_backend_names())
    assert set(BACKEND_ADAPTERS) == expected_backends
    assert BACKEND_ADAPTERS is BACKEND_REGISTRY
    assert expected_backends == set(BACKEND_REGISTRY)

    assert get_backend_adapter("gp").clear_table_sqls("schema.target") == [
        "TRUNCATE TABLE schema.target"
    ]
    assert get_backend_adapter("trino").clear_table_sqls("schema.target") == [
        "DELETE FROM schema.target"
    ]
    assert get_backend_adapter("ch").clear_table_sqls("db.target") == [
        "TRUNCATE TABLE IF EXISTS db.target"
    ]
    assert (
        get_backend_adapter("ch").drop_table_sql(
            "db.target",
            ch_cluster="{cluster}",
        )
        == "DROP TABLE IF EXISTS db.target ON CLUSTER '{cluster}'"
    )
    assert (
        get_backend_adapter("gp").build_insert_from_table_sql(
            "schema.target",
            "schema.stage",
            {"id": "BIGINT", "amount": "NUMERIC(12, 2)"},
        )
        == 'INSERT INTO schema.target ("id", "amount") '
        'SELECT CAST("id" AS BIGINT) AS "id", '
        'CAST("amount" AS NUMERIC(12, 2)) AS "amount" FROM schema.stage'
    )
    assert (
        get_backend_adapter("ch").count_table_rows_sql("db.target")
        == "SELECT count() FROM db.target"
    )
    assert get_backend_adapter("trino").build_dataframe_batch_insert_sql(
        "schema.stage",
        ["id", "name"],
        row_count=2,
    ) == ('INSERT INTO schema.stage ("id", "name") VALUES (?, ?), (?, ?)')
    assert get_backend_adapter("gp").build_stage_duplicate_keys_sql(
        "schema.stage",
        ["id", "dt"],
    ) == ('SELECT 1 FROM schema.stage GROUP BY "id", "dt" HAVING COUNT(*) > 1 LIMIT 1')
    assert get_backend_adapter("ch").build_stage_target_key_overlap_sql(
        "db.stage",
        "db.target",
        ["id"],
    ) == (
        "SELECT 1 FROM db.stage AS stage_src "
        "INNER JOIN db.target AS target_dst ON "
        "(stage_src.`id` = target_dst.`id` "
        "OR (stage_src.`id` IS NULL AND target_dst.`id` IS NULL)) "
        "LIMIT 1"
    )
    assert get_backend_adapter("gp").quote_identifier('a"b') == '"a""b"'
    assert get_backend_adapter("trino").quote_identifier('a"b') == '"a""b"'
    assert get_backend_adapter("ch").quote_identifier("a`b") == "`a``b`"


def test_backend_adapters_own_analyze_support_policy() -> None:
    assert get_backend_adapter("gp").should_analyze_table() is True
    assert get_backend_adapter("trino").should_analyze_table() is True
    assert get_backend_adapter("ch").should_analyze_table() is False


def test_registered_backends_implement_full_contract() -> None:
    required_methods = {
        "build_connection_config",
        "build_create_table_sqls",
        "copy_airflow_fields",
        "open_connection",
        "execute_command",
        "table_exists",
        "clear_table_sqls",
        "get_table_column_types",
        "inspect_source_query_schema",
        "map_source_type_to_target",
        "build_upsert_stage_sqls",
        "build_upsert_stage_placeholder_sqls",
        "execute_sql",
        "execute_read_sql",
        "insert_dataframe_batch",
        "insert_rows_batch",
        "running_query_ids_sql",
        "cancel_query_sql",
        "infer_dataframe_column_type",
    }
    inherited_contract_methods = {
        "build_insert_from_stage_sql",
        "build_insert_from_stage_placeholder_sql",
        "allows_show_tables_catalog_filter",
        "can_create_transfer_target_before_batches",
        "create_table_from_sql_fast_path",
        "build_create_from_sql_target_create_kwargs",
        "build_load_target_create_kwargs",
        "column_types_for_columns",
        "after_create_table",
        "expected_create_table_column_types",
        "requires_load_target_column_metadata",
        "refine_stage_column_types_from_rows",
        "needs_upsert_partition_drop_template",
        "normalize_ch_columns_or_expression",
        "normalize_ch_string",
        "resolve_ch_retry_per_host_drops",
        "resolve_transfer_stage_column_types",
        "resolve_transfer_staging_mode",
        "resolve_table_info_table_name",
        "should_analyze_table",
        "should_ensure_load_target_table",
        "should_insert_create_table_from_sql_directly",
        "supports_distributed_table_targets",
        "target_connection_defaults",
        "transfer_attempt_policy",
        "transfer_insert_page_sizing",
        "uses_partition_replacement_upsert",
        "validate_ch_create_table_options",
        "validate_ch_columns_in_columns",
        "validate_gp_distributed_by_key_option",
        "validate_gp_insert_chunk_size_option",
        "validate_trino_insert_chunk_size_option",
        "validate_write_mode",
    }
    missing: list[str] = []
    for backend_name, backend in BACKEND_REGISTRY.items():
        capability = get_backend_capability(backend_name)
        assert capability.name == backend_name
        assert capability == backend.capability
        assert backend.backend == backend_name
        for method_name in sorted(inherited_contract_methods):
            assert callable(getattr(backend, method_name))
        for method_name in sorted(required_methods):
            method = getattr(type(backend), method_name, None)
            if method is getattr(BackendAdapter, method_name, None):
                missing.append(f"{backend_name}.{method_name}")

    assert missing == []


def test_backend_transfer_and_load_policies_are_adapter_owned() -> None:
    gp_adapter = get_backend_adapter("gp")
    trino_adapter = get_backend_adapter("trino")
    ch_adapter = get_backend_adapter("ch")

    assert gp_adapter.target_connection_defaults(SimpleNamespace()).insert_chunk_size is None
    trino_defaults = trino_adapter.target_connection_defaults(
        SimpleNamespace(
            insert_chunk_size=123,
            s3_transfer_staging_location="s3://bucket/stage",
            upsert_partition_drop_sql_template="DELETE FROM {table}",
        )
    )
    assert trino_defaults.insert_chunk_size == 123
    assert trino_defaults.s3_transfer_staging_location == "s3://bucket/stage"
    assert trino_defaults.upsert_partition_drop_sql_template == "DELETE FROM {table}"

    gp_policy = gp_adapter.transfer_attempt_policy(retry_cnt=5)
    assert gp_policy.insert_retry_cnt == 5
    assert gp_policy.retry_ambiguous_stage_load is False
    gp_sizing = gp_adapter.transfer_insert_page_sizing(gp_insert_chunk_size=None)
    assert gp_sizing is not None
    assert gp_sizing.initial_size == 10_000
    assert gp_sizing.min_size == 1_000
    assert gp_sizing.max_size == 100_000
    explicit_gp_sizing = gp_adapter.transfer_insert_page_sizing(
        gp_insert_chunk_size=50_000,
    )
    assert explicit_gp_sizing is not None
    assert explicit_gp_sizing.initial_size == 50_000
    assert explicit_gp_sizing.min_size == 1_000
    assert explicit_gp_sizing.max_size == 200_000

    for adapter in (trino_adapter, ch_adapter):
        policy = adapter.transfer_attempt_policy(retry_cnt=5)
        assert policy.insert_retry_cnt == 1
        assert policy.retry_ambiguous_stage_load is True
        assert adapter.transfer_insert_page_sizing(gp_insert_chunk_size=None) is None

    assert (
        trino_adapter.requires_load_target_column_metadata(
            write_mode="replace",
            original_target_exists=False,
        )
        is True
    )
    assert (
        gp_adapter.requires_load_target_column_metadata(
            write_mode="replace",
            original_target_exists=True,
        )
        is False
    )
    assert (
        gp_adapter.requires_load_target_column_metadata(
            write_mode="upsert",
            original_target_exists=True,
        )
        is True
    )
    assert gp_adapter.uses_partition_replacement_upsert() is False
    assert trino_adapter.uses_partition_replacement_upsert() is True
    assert ch_adapter.uses_partition_replacement_upsert() is True
    assert gp_adapter.needs_upsert_partition_drop_template() is False
    assert trino_adapter.needs_upsert_partition_drop_template() is True
    assert ch_adapter.needs_upsert_partition_drop_template() is False
    assert gp_adapter.supports_distributed_table_targets() is False
    assert trino_adapter.supports_distributed_table_targets() is False
    assert ch_adapter.supports_distributed_table_targets() is True
    assert gp_adapter.can_create_transfer_target_before_batches() is True
    assert trino_adapter.can_create_transfer_target_before_batches() is True
    assert ch_adapter.can_create_transfer_target_before_batches() is False
    assert gp_adapter.allows_show_tables_catalog_filter() is False
    assert trino_adapter.allows_show_tables_catalog_filter() is True
    assert ch_adapter.allows_show_tables_catalog_filter() is False
    assert gp_adapter.validate_write_mode("append") == "append"
    assert trino_adapter.validate_write_mode("UPSERT") == "upsert"
    with pytest.raises(ValueError, match="must be one of"):
        ch_adapter.validate_write_mode("merge")
    original_gp_modes = gp_adapter.supported_write_modes
    gp_adapter.supported_write_modes = frozenset({"append"})
    try:
        with pytest.raises(ValueError, match="Greenplum does not support"):
            gp_adapter.validate_write_mode("upsert")
    finally:
        gp_adapter.supported_write_modes = original_gp_modes
    assert ch_adapter.normalize_ch_string(" id ", "order_by") == "id"
    assert ch_adapter.normalize_ch_columns_or_expression(
        [" id ", "dt"],
        "order_by",
    ) == ["id", "dt"]
    with pytest.raises(ValueError, match="duplicate column names"):
        ch_adapter.normalize_ch_columns_or_expression(["id", " id "], "order_by")
    ch_adapter.validate_ch_columns_in_columns(
        ["id"],
        ["id", "dt"],
        "order_by",
        data_name="staged data",
    )
    with pytest.raises(ValueError, match="missing"):
        ch_adapter.validate_ch_columns_in_columns(
            ["missing"],
            ["id"],
            "order_by",
            data_name="staged data",
        )
    assert gp_adapter.resolve_ch_retry_per_host_drops(True) is False
    assert trino_adapter.resolve_ch_retry_per_host_drops(True) is False
    assert ch_adapter.resolve_ch_retry_per_host_drops(True) is True
    assert ch_adapter.resolve_ch_retry_per_host_drops(False) is False
    create_batch = pd.DataFrame({"id": [1], "label": ["a"]})
    assert (
        gp_adapter.expected_create_table_column_types(
            create_batch,
            {"id": "BIGINT", "label": "TEXT"},
            ch_distributed_table=True,
            ch_only_shard=False,
        )
        is None
    )
    assert ch_adapter.expected_create_table_column_types(
        create_batch,
        {"id": "UInt64", "label": "String"},
        ch_distributed_table=True,
        ch_only_shard=False,
    ) == {"id": "UInt64", "label": "String"}
    assert (
        ch_adapter.expected_create_table_column_types(
            create_batch,
            {"id": "UInt64", "label": "String"},
            ch_distributed_table=False,
            ch_only_shard=False,
        )
        is None
    )
    assert (
        ch_adapter.expected_create_table_column_types(
            create_batch,
            {"id": "UInt64", "label": "String"},
            ch_distributed_table=True,
            ch_only_shard=True,
        )
        is None
    )

    gp_adapter.validate_gp_distributed_by_key_option(["id"], option_owner="to_db")
    gp_adapter.validate_gp_insert_chunk_size_option(1, option_owner="to_db")
    with pytest.raises(ValueError, match="positive integer"):
        gp_adapter.validate_gp_insert_chunk_size_option(0, option_owner="to_db")
    with pytest.raises(ValueError, match="to_db has type 'gp'"):
        trino_adapter.validate_gp_distributed_by_key_option(
            ["id"],
            option_owner="to_db",
        )
    with pytest.raises(ValueError, match="to_db has type 'gp'"):
        ch_adapter.validate_gp_insert_chunk_size_option(1000, option_owner="to_db")
    trino_adapter.validate_trino_insert_chunk_size_option(100, option_owner="to_db")
    with pytest.raises(ValueError, match="positive integer"):
        trino_adapter.validate_trino_insert_chunk_size_option(0, option_owner="to_db")
    gp_adapter.validate_trino_insert_chunk_size_option(1000, option_owner="to_db")
    ch_adapter.validate_trino_insert_chunk_size_option(1000, option_owner="to_db")
    with pytest.raises(ValueError, match="positive integer"):
        gp_adapter.validate_trino_insert_chunk_size_option(0, option_owner="to_db")
    with pytest.raises(ValueError, match="positive integer"):
        ch_adapter.validate_trino_insert_chunk_size_option(0, option_owner="to_db")
    ch_adapter.validate_ch_create_table_options(
        option_owner="to_db",
        partition_by=["dt"],
        order_by=["id"],
        ch_engine="ReplacingMergeTree",
        ch_cluster="cluster",
        ch_sharding_key="cityHash64(id)",
        ch_only_shard=True,
    )
    with pytest.raises(ValueError, match="ch_only_shard must be a boolean"):
        ch_adapter.validate_ch_create_table_options(
            option_owner="to_db",
            partition_by=None,
            order_by=None,
            ch_engine="ReplacingMergeTree",
            ch_cluster="cluster",
            ch_sharding_key="cityHash64(id)",
            ch_only_shard="yes",  # type: ignore[arg-type]
        )
    trino_adapter.validate_ch_create_table_options(
        option_owner="to_db",
        partition_by=["dt"],
        order_by=["id"],
        ch_engine="ReplicatedMergeTree",
        ch_cluster="{cluster}",
        ch_sharding_key="rand()",
        ch_only_shard=False,
    )
    with pytest.raises(ValueError, match="to_db has type 'ch'"):
        trino_adapter.validate_ch_create_table_options(
            option_owner="to_db",
            partition_by=None,
            order_by=None,
            ch_engine="ReplacingMergeTree",
            ch_cluster="{cluster}",
            ch_sharding_key="rand()",
            ch_only_shard=False,
        )
    with pytest.raises(ValueError, match="to_db has type 'gp'"):
        gp_adapter.validate_ch_create_table_options(
            option_owner="to_db",
            partition_by=None,
            order_by=["id"],
            ch_engine="ReplicatedMergeTree",
            ch_cluster="{cluster}",
            ch_sharding_key="rand()",
            ch_only_shard=False,
        )

    assert (
        trino_adapter.resolve_transfer_staging_mode(
            None,
            s3_transfer_staging_schema="tmp",
            s3_transfer_staging_location="s3://bucket/prefix",
        )
        == "parquet"
    )
    assert (
        trino_adapter.resolve_transfer_staging_mode(
            None,
            s3_transfer_staging_schema=None,
            s3_transfer_staging_location=None,
        )
        == "values"
    )
    with pytest.raises(ValueError, match="requires s3_transfer_staging_location"):
        trino_adapter.resolve_transfer_staging_mode(
            "parquet",
            s3_transfer_staging_schema="tmp",
            s3_transfer_staging_location=None,
        )
    with pytest.raises(ValueError, match="can only be used"):
        ch_adapter.resolve_transfer_staging_mode(
            "values",
            s3_transfer_staging_schema=None,
            s3_transfer_staging_location=None,
        )

    assert (
        gp_adapter.should_insert_create_table_from_sql_directly(
            source_backend="gp",
            source_key="source_gp",
            target_key="target_gp",
        )
        is True
    )
    assert (
        gp_adapter.should_insert_create_table_from_sql_directly(
            source_backend="trino",
            source_key="source_trino",
            target_key="target_gp",
        )
        is False
    )


def test_target_create_kwargs_are_backend_adapter_owned() -> None:
    gp_adapter = get_backend_adapter("gp")
    ch_adapter = get_backend_adapter("ch")

    assert gp_adapter.should_ensure_load_target_table(target_exists=True) is False
    assert gp_adapter.build_load_target_create_kwargs(
        gp_distributed_by_key=["id"],
        partition_by="dt",
        order_by=None,
        ch_engine="ReplicatedMergeTree",
        ch_cluster="{cluster}",
        ch_sharding_key="rand()",
        ch_only_shard=False,
        write_mode="replace",
        original_target_exists=True,
    ) == {
        "gp_distributed_by_key": ["id"],
        "partition_by": "dt",
    }

    assert ch_adapter.should_ensure_load_target_table(target_exists=True) is True
    assert ch_adapter.build_load_target_create_kwargs(
        gp_distributed_by_key=None,
        partition_by="toYYYYMM(dt)",
        order_by=["id"],
        ch_engine="MergeTree",
        ch_cluster="cluster",
        ch_sharding_key="id",
        ch_only_shard=False,
        write_mode="replace",
        original_target_exists=True,
    ) == {
        "gp_distributed_by_key": None,
        "partition_by": "toYYYYMM(dt)",
        "order_by": ["id"],
        "ch_engine": "MergeTree",
        "ch_cluster": "cluster",
        "ch_sharding_key": "id",
        "ch_distributed_table": True,
        "ch_only_shard": False,
        "ch_replace_table": True,
    }
    assert (
        ch_adapter.build_create_from_sql_target_create_kwargs(
            gp_distributed_by_key=None,
            partition_by=None,
            order_by=None,
            ch_engine="MergeTree",
            ch_cluster="cluster",
            ch_sharding_key="id",
            ch_only_shard=True,
            drop_target_if_exists=True,
            target_exists_before_drop=True,
        )["ch_distributed_table"]
        is False
    )


def test_trino_parquet_stage_helpers_are_adapter_owned() -> None:
    adapter = get_backend_adapter("trino")
    create_sql = adapter.build_parquet_stage_table_sql(
        "hive.tmp.stage",
        {
            "id": "BIGINT",
            "amount": "DECIMAL(3, 2)",
            "created_at": "TIMESTAMP(3)",
            "event_ts": "TIMESTAMP(6) WITH TIME ZONE",
            "row_uuid": "UUID",
            "label": "VARCHAR",
        },
        "s3://bucket/stage/target's/",
        query_label="load-parquet",
    )

    assert create_sql == (
        "/* analytics_toolkit query_label=load-parquet */\n"
        'CREATE TABLE hive.tmp.stage ("id" BIGINT, "amount" DECIMAL(3, 2), '
        '"created_at" TIMESTAMP(6), "event_ts" VARCHAR, "row_uuid" VARCHAR, '
        '"label" VARCHAR) '
        "WITH (format = 'PARQUET', "
        "external_location = 's3://bucket/stage/target''s/')"
    )
    assert adapter.parquet_stage_target_table_base("catalog.schema.target") == "target"

    batch = SimpleNamespace(
        columns=[
            "flag",
            "id",
            "ratio",
            "amount",
            "created_at",
            "event_dt",
            "payload",
            "label",
            "empty",
        ],
        rows=[
            (
                True,
                7,
                1.25,
                Decimal("1.23"),
                datetime(2026, 1, 2, 3, 4, 5),
                date(2026, 1, 2),
                b"x",
                "ok",
                None,
            )
        ],
    )
    assert adapter.infer_parquet_stage_column_types_from_rows(batch) == {
        "flag": "BOOLEAN",
        "id": "BIGINT",
        "ratio": "DOUBLE",
        "amount": "DECIMAL(3, 2)",
        "created_at": "TIMESTAMP",
        "event_dt": "DATE",
        "payload": "VARBINARY",
        "label": "VARCHAR",
        "empty": "VARCHAR",
    }


def test_dataframe_column_type_inference_is_adapter_owned() -> None:
    gp_adapter = get_backend_adapter("gp")
    trino_adapter = get_backend_adapter("trino")
    ch_adapter = get_backend_adapter("ch")

    assert gp_adapter.infer_dataframe_column_type(pd.Series([1, 2])) == "BIGINT"
    assert gp_adapter.infer_dataframe_column_type(pd.Series([1.5, 2.5])) == "DOUBLE PRECISION"
    assert trino_adapter.infer_dataframe_column_type(pd.Series([1.5, 2.5])) == "DOUBLE"
    assert trino_adapter.infer_dataframe_column_type(pd.Series(["a", "b"])) == "VARCHAR"
    assert ch_adapter.infer_dataframe_column_type(pd.Series([1, None])) == ("Nullable(Float64)")
    assert (
        ch_adapter.infer_dataframe_column_type(pd.Series([Decimal("1.2"), Decimal("3.4")]))
        == "Float64"
    )


def test_backend_lookup_preserves_connection_config_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    connections_path = tmp_path / ".connections"
    connections_path.unlink()
    with pytest.raises(SqlConfigError, match="Missing SQL connections file"):
        get_backend("missing_alias")

    connections_path.write_text("{", encoding="utf-8")
    with pytest.raises(SqlConfigError, match="must contain valid JSON"):
        get_backend("missing_alias")


def test_backend_lookup_preserves_unknown_connection_key_errors() -> None:
    with pytest.raises(UnsupportedConnectionTypeError, match="Unknown SQL connection key"):
        get_backend("missing_alias")


def test_sql_backend_dispatch_uses_adapter_boundary() -> None:
    dispatch_functions = [
        read_sql_module._read_backend,
        execute_sql_module._execute_backend,
        execute_read_module._execute_read_backend,
        load_sql_table_module._insert_batch_backend,
        load_sql_table_module._insert_rows_backend,
    ]
    for function in dispatch_functions:
        assert "globals()[" not in inspect.getsource(function)

    assert "get_backend_adapter" in inspect.getsource(read_sql_module._read_backend)
    assert "get_backend_adapter" in inspect.getsource(execute_sql_module._execute_backend)
    assert "get_backend_adapter" in inspect.getsource(execute_read_module._execute_read_backend)
    assert not hasattr(read_sql_module, "_READ_BACKENDS")
    assert not hasattr(execute_sql_module, "_EXECUTE_BACKENDS")
    assert not hasattr(execute_read_module, "_EXECUTE_READ_BACKENDS")
    assert not hasattr(load_sql_table_module, "_BATCH_INSERT_BACKENDS")
    assert not hasattr(load_sql_table_module, "_ROW_INSERT_BACKENDS")
    assert "get_backend_adapter" in inspect.getsource(load_sql_table_module._insert_batch_backend)
    assert "get_backend_adapter" in inspect.getsource(load_sql_table_module._insert_rows_backend)


def test_backend_adapters_read_dataframes_for_dbapi_and_clickhouse() -> None:
    gp_connection = FakeDbapiConnection(
        rows=[(1, "ok")],
        description=[("id",), ("label",)],
    )
    printed: list[tuple[str, bool]] = []

    gp_result = get_backend_adapter("gp").read_dataframe(
        gp_connection,
        "select id, label",
        print_queries=True,
        print_query=lambda query, enabled: printed.append((query, enabled)),
        read_dbapi_query=read_sql_module._read_dbapi_query,
    )

    pd.testing.assert_frame_equal(
        gp_result,
        pd.DataFrame({"id": [1], "label": ["ok"]}),
    )
    assert printed == [("select id, label", True)]
    assert gp_connection.executed == ["select id, label"]

    class ReadClickHouseClient:
        def __init__(self) -> None:
            self.queries: list[str] = []

        def query_df(self, sql: str) -> pd.DataFrame:
            self.queries.append(sql)
            return pd.DataFrame({"value": [2]})

    ch_client = ReadClickHouseClient()
    ch_result = get_backend_adapter("ch").read_dataframe(
        ch_client,
        "select value",
        print_queries=False,
        print_query=lambda query, enabled: printed.append((query, enabled)),
        read_dbapi_query=lambda connection, query: pytest.fail("ClickHouse should use query_df"),
    )

    pd.testing.assert_frame_equal(ch_result, pd.DataFrame({"value": [2]}))
    assert ch_client.queries == ["select value"]


def test_backend_adapters_execute_operations_like_existing_table_ops() -> None:
    gp_connection = FakeDbapiConnection(rows=[(5,)])
    get_backend_adapter("gp").clear_table(gp_connection, "schema.target")
    assert gp_connection.executed == ["TRUNCATE TABLE schema.target"]
    assert gp_connection.commit_calls == 1

    trino_connection = FakeDbapiConnection(rows=[(7,)])
    assert (
        get_backend_adapter("trino").count_table_rows(
            trino_connection,
            "schema.target",
        )
        == 7
    )
    assert trino_connection.executed == ["SELECT COUNT(*) FROM schema.target"]
    assert trino_connection.commit_calls == 0

    ch_client = RecordingClickHouseClient()
    get_backend_adapter("ch").drop_table(
        ch_client,
        "db.target",
        ch_cluster="{cluster}",
    )
    assert ch_client.commands == [
        (
            "DROP TABLE IF EXISTS db.target ON CLUSTER '{cluster}'",
            {
                "distributed_ddl_task_timeout": 0,
                "distributed_ddl_output_mode": "none",
            },
        )
    ]
    assert get_backend_adapter("ch").count_table_rows(ch_client, "db.target") == 9
    assert ch_client.queries[-1] == "SELECT count() FROM db.target"


def test_clickhouse_lifecycle_builds_distributed_pair_sql_in_order() -> None:
    assert ch_lifecycle_module.build_drop_ch_distributed_table_pair_sqls(
        "db.target",
        ch_cluster="{cluster}",
    ) == [
        "DROP TABLE IF EXISTS db.target",
        "DROP TABLE IF EXISTS db.target_shard",
        "DROP TABLE IF EXISTS db.target ON CLUSTER '{cluster}'",
        "DROP TABLE IF EXISTS db.target_shard ON CLUSTER '{cluster}'",
    ]

    assert ch_lifecycle_module.build_truncate_ch_distributed_table_pair_sqls(
        "db.target",
        ch_cluster="analytics",
    ) == [
        "TRUNCATE TABLE IF EXISTS db.target_shard ON CLUSTER analytics",
        "TRUNCATE TABLE IF EXISTS db.target",
    ]

    create_sqls = ch_lifecycle_module.build_create_ch_distributed_table_pair_sqls(
        table_name="db.target",
        joined_columns="`id` UInt64",
        partition_by=["id"],
        order_by=["id"],
        ch_cluster="{cluster}",
        ch_sharding_key="cityHash64(id)",
    )
    assert len(create_sqls) == 4
    assert create_sqls[0].startswith("CREATE TABLE IF NOT EXISTS db.target_shard")
    assert "ON CLUSTER '{cluster}'" in create_sqls[0]
    assert create_sqls[1].startswith("CREATE TABLE IF NOT EXISTS db.target_shard")
    assert "ON CLUSTER" not in create_sqls[1]
    assert "UUID '" in create_sqls[1]
    assert create_sqls[2].startswith("CREATE TABLE IF NOT EXISTS db.target")
    assert "ON CLUSTER '{cluster}'" in create_sqls[2]
    assert create_sqls[3].startswith("CREATE TABLE IF NOT EXISTS db.target")
    assert "ON CLUSTER" not in create_sqls[3]


def test_clickhouse_lifecycle_executes_on_cluster_settings() -> None:
    client = RecordingClickHouseClient()

    ch_lifecycle_module.drop_ch_distributed_table_pair(
        client,
        "db.target",
        ch_cluster="{cluster}",
    )

    assert client.commands[2] == (
        "DROP TABLE IF EXISTS db.target ON CLUSTER '{cluster}'",
        {
            "distributed_ddl_task_timeout": 0,
            "distributed_ddl_output_mode": "none",
        },
    )
    assert client.queries == []


def test_clickhouse_lifecycle_retries_drop_on_cluster_hosts() -> None:
    class RootClient(RecordingClickHouseClient):
        def __init__(self) -> None:
            super().__init__()
            self.table_queries = 0

        def query(self, sql: str) -> FakeClickHouseResult:
            self.queries.append(sql)
            if sql.startswith("SELECT getMacro("):
                return FakeClickHouseResult([("core",)])
            if "clusterAllReplicas" in sql and "system, one" in sql:
                return FakeClickHouseResult([(2,)])
            if sql.startswith("SELECT count()") and "FROM system.clusters" in sql:
                return FakeClickHouseResult([(2,)])
            if sql.startswith("SELECT DISTINCT host_name"):
                return FakeClickHouseResult([("host-a",), ("host-b",)])
            if "clusterAllReplicas" in sql and "system, tables" in sql:
                self.table_queries += 1
                if self.table_queries <= 2:
                    return FakeClickHouseResult(
                        [("host-b", "db", "target_shard", "ReplicatedMergeTree")]
                    )
                return FakeClickHouseResult([])
            return FakeClickHouseResult([])

    class HostClient(RecordingClickHouseClient):
        def __init__(self, host: str) -> None:
            super().__init__()
            self.host = host
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1

    root_client = RootClient()
    host_clients: dict[str, HostClient] = {}

    def host_factory(host: str) -> HostClient:
        host_client = HostClient(host)
        host_clients[host] = host_client
        return host_client

    ch_lifecycle_module.drop_ch_distributed_table_pair(
        root_client,
        "db.target",
        ch_cluster="{cluster}",
        wait_for_absence=True,
        wait_timeout_seconds=0,
        wait_poll_interval_seconds=0,
        ch_retry_per_host_drops=True,
        per_host_connection_factory=host_factory,
    )

    assert set(host_clients) == {"host-b"}
    assert host_clients["host-b"].commands == [
        ("DROP TABLE IF EXISTS db.target", None),
        ("DROP TABLE IF EXISTS db.target_shard", None),
    ]
    assert host_clients["host-b"].close_calls == 1
    assert root_client.table_queries == 3


def test_clickhouse_lifecycle_retries_all_hosts_when_leftover_host_unmapped() -> None:
    class RootClient(RecordingClickHouseClient):
        def __init__(self) -> None:
            super().__init__()
            self.table_queries = 0

        def query(self, sql: str) -> FakeClickHouseResult:
            self.queries.append(sql)
            if sql.startswith("SELECT getMacro("):
                return FakeClickHouseResult([("core",)])
            if "clusterAllReplicas" in sql and "system, one" in sql:
                return FakeClickHouseResult([(2,)])
            if sql.startswith("SELECT count()") and "FROM system.clusters" in sql:
                return FakeClickHouseResult([(2,)])
            if sql.startswith("SELECT DISTINCT host_name"):
                return FakeClickHouseResult([("host-a",), ("host-b",)])
            if "clusterAllReplicas" in sql and "system, tables" in sql:
                self.table_queries += 1
                if self.table_queries <= 2:
                    return FakeClickHouseResult(
                        [
                            (
                                "clickhouse-01",
                                "db",
                                "target_shard",
                                "ReplicatedMergeTree",
                            )
                        ]
                    )
                return FakeClickHouseResult([])
            return FakeClickHouseResult([])

    class HostClient(RecordingClickHouseClient):
        def __init__(self, host: str) -> None:
            super().__init__()
            self.host = host

    root_client = RootClient()
    host_clients: dict[str, HostClient] = {}

    def host_factory(host: str) -> HostClient:
        host_client = HostClient(host)
        host_clients[host] = host_client
        return host_client

    ch_lifecycle_module.drop_ch_distributed_table_pair(
        root_client,
        "db.target",
        ch_cluster="{cluster}",
        wait_for_absence=True,
        wait_timeout_seconds=0,
        wait_poll_interval_seconds=0,
        ch_retry_per_host_drops=True,
        per_host_connection_factory=host_factory,
    )

    assert set(host_clients) == {"host-a", "host-b"}
    for host_client in host_clients.values():
        assert host_client.commands == [
            ("DROP TABLE IF EXISTS db.target", None),
            ("DROP TABLE IF EXISTS db.target_shard", None),
        ]
    assert root_client.table_queries == 3


def test_clickhouse_lifecycle_retries_per_host_drops_concurrently() -> None:
    class RootClient(RecordingClickHouseClient):
        def __init__(self) -> None:
            super().__init__()
            self.table_queries = 0

        def query(self, sql: str) -> FakeClickHouseResult:
            self.queries.append(sql)
            if sql.startswith("SELECT getMacro("):
                return FakeClickHouseResult([("core",)])
            if "clusterAllReplicas" in sql and "system, one" in sql:
                return FakeClickHouseResult([(2,)])
            if sql.startswith("SELECT count()") and "FROM system.clusters" in sql:
                return FakeClickHouseResult([(2,)])
            if sql.startswith("SELECT DISTINCT host_name"):
                return FakeClickHouseResult([("host-a",), ("host-b",)])
            if "clusterAllReplicas" in sql and "system, tables" in sql:
                self.table_queries += 1
                if self.table_queries <= 2:
                    return FakeClickHouseResult(
                        [
                            ("host-a", "db", "target_shard", "ReplicatedMergeTree"),
                            ("host-b", "db", "target_shard", "ReplicatedMergeTree"),
                        ]
                    )
                return FakeClickHouseResult([])
            return FakeClickHouseResult([])

    active = 0
    max_active = 0
    active_lock = threading.Lock()
    barrier = threading.Barrier(2, timeout=5)

    class HostClient(RecordingClickHouseClient):
        def command(
            self,
            sql: str,
            settings: dict[str, object] | None = None,
        ) -> dict[str, int] | None:
            nonlocal active, max_active
            if sql == "DROP TABLE IF EXISTS db.target":
                with active_lock:
                    active += 1
                    max_active = max(max_active, active)
                try:
                    barrier.wait()
                finally:
                    with active_lock:
                        active -= 1
            return super().command(sql, settings=settings)

    host_clients: dict[str, HostClient] = {}

    def host_factory(host: str) -> HostClient:
        host_client = HostClient()
        host_clients[host] = host_client
        return host_client

    ch_lifecycle_module.drop_ch_distributed_table_pair(
        RootClient(),
        "db.target",
        ch_cluster="{cluster}",
        wait_for_absence=True,
        wait_timeout_seconds=0,
        wait_poll_interval_seconds=0,
        ch_retry_per_host_drops=True,
        per_host_connection_factory=host_factory,
    )

    assert set(host_clients) == {"host-a", "host-b"}
    assert max_active == 2


def test_clickhouse_lifecycle_drop_leftovers_mentions_per_host_retry() -> None:
    class LeftoverClient(RecordingClickHouseClient):
        def query(self, sql: str) -> FakeClickHouseResult:
            self.queries.append(sql)
            if sql.startswith("SELECT getMacro("):
                return FakeClickHouseResult([("core",)])
            if "clusterAllReplicas" in sql and "system, one" in sql:
                return FakeClickHouseResult([(1,)])
            if "FROM system.clusters" in sql:
                return FakeClickHouseResult([(1,)])
            if "clusterAllReplicas" in sql and "system, tables" in sql:
                return FakeClickHouseResult(
                    [("host-a", "db", "target_shard", "ReplicatedMergeTree")]
                )
            return FakeClickHouseResult([])

    with pytest.raises(TimeoutError) as exc_info:
        ch_lifecycle_module.drop_ch_distributed_table_pair(
            LeftoverClient(),
            "db.target",
            ch_cluster="{cluster}",
            wait_for_absence=True,
            wait_timeout_seconds=0,
            wait_poll_interval_seconds=0,
        )

    message = str(exc_info.value)
    assert "host-a: db.target_shard (ReplicatedMergeTree)" in message
    assert "ch_retry_per_host_drops=True" in message


def test_target_lifecycle_helper_preserves_non_ch_replace_modes() -> None:
    drop_connection = FakeDbapiConnection()
    target_exists = table_ops_module.apply_target_write_mode(
        "gp",
        drop_connection,
        "schema.target",
        write_mode="replace",
        target_exists=True,
        replace_existing_non_ch="drop",
    )
    assert target_exists is False
    assert drop_connection.executed == ["DROP TABLE IF EXISTS schema.target"]

    clear_connection = FakeDbapiConnection()
    target_exists = table_ops_module.apply_target_write_mode(
        "gp",
        clear_connection,
        "schema.target",
        write_mode="replace",
        target_exists=True,
        replace_existing_non_ch="clear",
    )
    assert target_exists is True
    assert clear_connection.executed == ["TRUNCATE TABLE schema.target"]


def test_target_lifecycle_can_preserve_load_df_ch_truncate_missing_target() -> None:
    client = RecordingClickHouseClient()

    target_exists = table_ops_module.apply_target_write_mode(
        "ch",
        client,
        "db.target",
        write_mode="truncate_insert",
        target_exists=False,
        replace_existing_non_ch="drop",
        drop_missing_ch_truncate_target=False,
    )

    assert target_exists is False
    assert client.commands == []


def test_backend_adapters_execute_validation_queries_per_backend() -> None:
    gp_connection = FakeDbapiConnection(rows=[(1,)])
    assert get_backend_adapter("gp").stage_has_duplicate_keys(
        gp_connection,
        "schema.stage",
        ["id"],
    )
    assert gp_connection.executed == [
        'SELECT 1 FROM schema.stage GROUP BY "id" HAVING COUNT(*) > 1 LIMIT 1'
    ]

    ch_client = RecordingClickHouseClient()
    assert (
        get_backend_adapter("ch").stage_keys_overlap_target(
            ch_client,
            "db.stage",
            "db.target",
            ["id"],
        )
        is False
    )
    assert ch_client.queries[-1] == (
        "SELECT 1 FROM db.stage AS stage_src "
        "INNER JOIN db.target AS target_dst ON "
        "(stage_src.`id` = target_dst.`id` "
        "OR (stage_src.`id` IS NULL AND target_dst.`id` IS NULL)) "
        "LIMIT 1"
    )


def test_stage_base_identifier_policy_is_adapter_owned() -> None:
    assert get_backend_adapter("trino").stage_base_identifier("target", None, "abcd") == "target"
    assert get_backend_adapter("ch").stage_base_identifier("target", "loader", "abcd") == "target"


def test_gp_stage_base_identifier_keeps_marker_within_backend_limit() -> None:
    adapter = get_backend_adapter("gp")

    identifier = adapter.stage_base_identifier(
        "very_long_target_table_name_for_monthly_analytics_exports",
        "karapsin_de",
        "4f99601c",
    )
    stage_identifier = f"{identifier}__analytics_toolkit_karapsin_de__stage__4f99601c"

    assert len(stage_identifier.encode()) <= GP_IDENTIFIER_MAX_BYTES
    assert stage_identifier.endswith("__stage__4f99601c")


def test_dbapi_backend_adapter_rolls_back_failed_committed_commands() -> None:
    class FailingCursor:
        def __init__(self, connection: FailingConnection) -> None:
            self.connection = connection

        def execute(self, sql: str) -> None:
            self.connection.executed.append(sql)
            raise RuntimeError("boom")

        def close(self) -> None:
            self.connection.cursor_closed = True

    class FailingConnection:
        def __init__(self) -> None:
            self.executed: list[str] = []
            self.commit_calls = 0
            self.rollback_calls = 0
            self.cursor_closed = False

        def cursor(self) -> FailingCursor:
            return FailingCursor(self)

        def commit(self) -> None:
            self.commit_calls += 1

        def rollback(self) -> None:
            self.rollback_calls += 1

    connection = FailingConnection()

    try:
        get_backend_adapter("gp").execute_command(connection, "DROP TABLE target")
    except RuntimeError:
        pass
    else:
        raise AssertionError("Expected failing execute to raise.")

    assert connection.executed == ["DROP TABLE target"]
    assert connection.commit_calls == 0
    assert connection.rollback_calls == 1
    assert connection.cursor_closed is True


def test_backend_adapter_insert_from_query_returns_backend_row_counts() -> None:
    class RowCountCursorConnection(FakeDbapiConnection):
        def __init__(self) -> None:
            super().__init__()
            self.insert_rowcount = 4

    gp_connection = RowCountCursorConnection()
    assert (
        get_backend_adapter("gp").insert_from_query(
            gp_connection,
            "schema.target",
            "select id from source",
            {"id": "BIGINT"},
        )
        == 4
    )
    assert gp_connection.commit_calls == 1

    ch_client = RecordingClickHouseClient()
    assert (
        get_backend_adapter("ch").insert_from_query(
            ch_client,
            "db.target",
            "select id from source",
            {"id": "Nullable(Int64)"},
        )
        == 3
    )
    assert ch_client.commands[-1][0] == (
        "INSERT INTO db.target (`id`) "
        "SELECT CAST(`id` AS Nullable(Int64)) AS `id` "
        "FROM (select id from source) AS source_query"
    )


def test_backend_registry_normalizes_aliases_and_reports_supported_names() -> None:
    assert normalize_backend_name(" PostgreSQL ") == "gp"
    assert normalize_backend_name("clickhouse-connect") == "ch"
    assert require_backend_name(" TRINO ", connection_key="warehouse") == "trino"
    assert supported_backend_message() == "Expected one of: ch, gp, trino."
    assert set(backend_capability_map()) == {"ch", "gp", "trino"}

    with pytest.raises(UnsupportedConnectionTypeError, match="backend 'Oracle'"):
        normalize_backend_name("Oracle")
    with pytest.raises(
        UnsupportedConnectionTypeError,
        match=r"connection 'warehouse'.*unsupported type 'postgres'",
    ):
        require_backend_name("postgres", connection_key="warehouse")


def test_backend_registry_rejects_invalid_backend_returned_by_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_module = importlib.import_module("analytics_toolkit.sql.connection.config")
    monkeypatch.setattr(
        config_module,
        "get_connection_backend",
        lambda connection_key: "oracle",
    )

    with pytest.raises(
        UnsupportedConnectionTypeError,
        match="Unsupported connection type",
    ):
        backend_registry_module.get_backend("warehouse")


def test_backend_validation_builds_multi_stage_duplicate_query() -> None:
    adapter = get_backend_adapter("gp")

    assert adapter.build_stage_duplicate_keys_sql_for_tables(
        ["stage.first", "stage.second"],
        ["id", "region"],
    ) == (
        "SELECT 1 FROM (\n"
        'SELECT "id", "region" FROM stage.first\n'
        "UNION ALL\n"
        'SELECT "id", "region" FROM stage.second\n'
        ') AS stage_src GROUP BY "id", "region" '
        "HAVING COUNT(*) > 1 LIMIT 1"
    )


def test_backend_validation_query_closes_cursor_when_execute_fails() -> None:
    query_error = RuntimeError("query failed")

    class Cursor:
        def __init__(self) -> None:
            self.closed = False

        def execute(self, sql: str) -> None:
            raise query_error

        def close(self) -> None:
            self.closed = True

    cursor = Cursor()
    connection = SimpleNamespace(cursor=lambda: cursor)

    with pytest.raises(RuntimeError, match="query failed"):
        backend_validation_module.query_has_rows(
            get_backend_adapter("gp"),
            connection,
            "SELECT 1",
        )

    assert cursor.closed is True


def test_backend_read_dataframe_logs_failed_sql(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    general_module = importlib.import_module("analytics_toolkit.general")
    messages: list[tuple[str, str | None]] = []
    monkeypatch.setattr(
        general_module,
        "time_print",
        lambda message, backend=None: messages.append((message, backend)),
    )

    with pytest.raises(RuntimeError, match="read failed"):
        get_backend_adapter("gp").read_dataframe(
            object(),
            "SELECT secret FROM source",
            print_queries=False,
            print_query=lambda query, enabled: None,
            read_dbapi_query=lambda connection, query: (_ for _ in ()).throw(
                RuntimeError("read failed")
            ),
        )

    assert messages == [
        ("Reading DataFrame", "gp"),
        ("Failed SQL:\nSELECT secret FROM source", "gp"),
    ]


class _SourceCountCursor:
    def __init__(
        self,
        *,
        fetchone: object | None = None,
        fetchall: object | None = None,
        rows: list[tuple[int, ...]] | None = None,
    ) -> None:
        if fetchone is not None:
            self.fetchone = fetchone
        if fetchall is not None:
            self.fetchall = fetchall
        if rows is not None:
            self._rows = rows


def test_source_count_helpers_cover_cursor_shapes_and_labels() -> None:
    assert get_backend_adapter("gp").strip_query_semicolon(" SELECT 1;  ") == "SELECT 1"
    assert backend_source_count_module.fetch_first_row(
        _SourceCountCursor(fetchone=lambda: (7,))
    ) == (7,)
    assert backend_source_count_module.fetch_first_row(
        _SourceCountCursor(fetchall=lambda: [(8,), (9,)])
    ) == (8,)
    assert backend_source_count_module.fetch_first_row(_SourceCountCursor(fetchall=list)) is None
    assert backend_source_count_module.fetch_first_row(_SourceCountCursor(rows=[(10,)])) == (10,)
    assert backend_source_count_module.fetch_first_row(_SourceCountCursor(rows=[])) is None
    with pytest.raises(TypeError, match="Cursor must provide"):
        backend_source_count_module.fetch_first_row(object())

    assert get_backend_adapter("gp").build_source_count_sql(
        "SELECT * FROM source;",
        query_label="count */ safely",
    ) == (
        "/* analytics_toolkit query_label=count * / safely */\n"
        "SELECT COUNT(*) FROM (SELECT * FROM source) AS source_count_probe"
    )


def test_materialized_transfer_source_sql_is_backend_specific() -> None:
    assert (
        get_backend_adapter("gp").build_materialize_transfer_source_sql(
            "scratch.result",
            "SELECT * FROM source;",
        )
        == "CREATE TABLE scratch.result AS SELECT * FROM source DISTRIBUTED RANDOMLY"
    )
    assert (
        get_backend_adapter("trino").build_materialize_transfer_source_sql(
            "scratch.result",
            "SELECT * FROM source;",
        )
        == "CREATE TABLE scratch.result AS SELECT * FROM source"
    )
    assert get_backend_adapter("ch").build_materialize_transfer_source_sql(
        "scratch.result",
        "SELECT * FROM source;",
    ) == ("CREATE TABLE scratch.result ENGINE = MergeTree ORDER BY tuple() AS SELECT * FROM source")


@pytest.mark.parametrize(
    ("series", "gp_type", "ch_type"),
    [
        (pd.Series([True, False]), "BOOLEAN", "Bool"),
        (
            pd.Series(pd.to_datetime(["2026-01-01", "2026-01-02"])),
            "TIMESTAMP",
            "DateTime64(6)",
        ),
        (pd.Series([date(2026, 1, 1), date(2026, 1, 2)]), "DATE", "Date"),
    ],
)
def test_dataframe_type_inference_covers_temporal_and_boolean_types(
    series: pd.Series,
    gp_type: str,
    ch_type: str,
) -> None:
    assert get_backend_adapter("gp").infer_dataframe_column_type(series) == gp_type
    assert get_backend_adapter("ch").infer_dataframe_column_type(series) == ch_type


def test_dataframe_type_inference_preserves_uuid_values() -> None:
    values = pd.Series([UUID(int=1), UUID(int=2)])
    nullable_values = pd.Series([UUID(int=1), None])
    mixed_values = pd.Series([UUID(int=1), "not-a-uuid"])

    assert get_backend_adapter("gp").infer_dataframe_column_type(values) == "UUID"
    assert get_backend_adapter("trino").infer_dataframe_column_type(values) == "UUID"
    assert get_backend_adapter("ch").infer_dataframe_column_type(values) == "UUID"
    assert get_backend_adapter("ch").infer_dataframe_column_type(nullable_values) == (
        "Nullable(UUID)"
    )
    assert get_backend_adapter("gp").infer_dataframe_column_type(mixed_values) == "TEXT"
    assert get_backend_adapter("trino").infer_dataframe_column_type(mixed_values) == "VARCHAR"
    assert get_backend_adapter("ch").infer_dataframe_column_type(mixed_values) == "String"


@pytest.mark.parametrize(
    ("backend", "expected"),
    [("gp", "UUID"), ("trino", "UUID"), ("ch", "Nullable(UUID)")],
)
def test_source_type_mapping_preserves_uuid(backend: str, expected: str) -> None:
    assert (
        get_backend_adapter(backend).map_source_type_to_target(
            SourceColumn("value", "Nullable(UUID)")
        )
        == expected
    )


def test_dbapi_execute_commands_rolls_back_and_closes_on_later_failure() -> None:
    command_error = RuntimeError("command failed")

    class Cursor:
        def __init__(self, connection: Connection) -> None:
            self.connection = connection

        def execute(self, sql: str) -> None:
            self.connection.executed.append(sql)
            if sql == "bad":
                raise command_error

        def close(self) -> None:
            self.connection.closed = True

    class Connection:
        def __init__(self) -> None:
            self.executed: list[str] = []
            self.rollback_calls = 0
            self.closed = False

        def cursor(self) -> Cursor:
            return Cursor(self)

        def rollback(self) -> None:
            self.rollback_calls += 1

    connection = Connection()

    with pytest.raises(RuntimeError, match="command failed"):
        get_backend_adapter("gp").execute_commands(connection, ["good", "bad"])

    assert connection.executed == ["good", "bad"]
    assert connection.rollback_calls == 1
    assert connection.closed is True


def test_default_execute_commands_dispatches_every_statement() -> None:
    calls: list[tuple[object, str]] = []
    connection = object()
    adapter = SimpleNamespace(
        execute_command=lambda current_connection, sql: calls.append((current_connection, sql))
    )

    backend_common_methods_module.execute_commands(
        adapter,
        connection,
        ["SELECT 1", "SELECT 2"],
    )

    assert calls == [(connection, "SELECT 1"), (connection, "SELECT 2")]


def test_noncommitting_dbapi_failures_do_not_require_rollback() -> None:
    execute_error = RuntimeError("failed")

    class Cursor:
        def __init__(self) -> None:
            self.closed = False

        def execute(self, sql: str) -> None:
            raise execute_error

        def close(self) -> None:
            self.closed = True

    cursor = Cursor()
    connection = SimpleNamespace(cursor=lambda: cursor)
    adapter = get_backend_adapter("trino")

    with pytest.raises(RuntimeError, match="failed"):
        adapter.execute_command(connection, "SELECT 1")
    assert cursor.closed is True

    cursor.closed = False
    with pytest.raises(RuntimeError, match="failed"):
        adapter.execute_commands(connection, ["SELECT 2"])
    assert cursor.closed is True


def test_trino_materialization_command_drains_results_before_close() -> None:
    events: list[str] = []

    class Cursor:
        def execute(self, sql: str) -> None:
            events.append(f"execute:{sql}")

        def fetchall(self) -> list[object]:
            events.append("fetchall")
            return []

        def close(self) -> None:
            events.append("close")

    get_backend_adapter("trino").execute_materialization_command(
        SimpleNamespace(cursor=Cursor),
        "CREATE TABLE snapshot AS SELECT 1",
    )

    assert events == [
        "execute:CREATE TABLE snapshot AS SELECT 1",
        "fetchall",
        "close",
    ]


def test_dbapi_insert_from_query_rolls_back_failed_committed_insert() -> None:
    insert_error = RuntimeError("insert failed")

    class Cursor:
        def __init__(self, connection: Connection) -> None:
            self.connection = connection

        def execute(self, sql: str) -> None:
            self.connection.sql = sql
            raise insert_error

        def close(self) -> None:
            self.connection.closed = True

    class Connection:
        def __init__(self) -> None:
            self.sql = ""
            self.rollback_calls = 0
            self.closed = False

        def cursor(self) -> Cursor:
            return Cursor(self)

        def rollback(self) -> None:
            self.rollback_calls += 1

    connection = Connection()

    with pytest.raises(RuntimeError, match="insert failed"):
        get_backend_adapter("gp").insert_from_query(
            connection,
            "target",
            "SELECT id FROM source",
            {"id": "BIGINT"},
        )

    assert connection.sql.startswith("INSERT INTO target")
    assert connection.rollback_calls == 1
    assert connection.closed is True


def test_gp_stage_identifier_rejects_marker_larger_than_identifier_limit() -> None:
    with pytest.raises(ValueError, match="marker is too long"):
        get_backend_adapter("gp").stage_base_identifier(
            "target",
            "x" * GP_IDENTIFIER_MAX_BYTES,
            "suffix",
        )


def test_gp_identifier_byte_helpers_cover_tiny_and_multibyte_limits() -> None:
    assert gp_stage_module._fit_identifier_bytes("very-long-name", 4) == "458f"
    assert gp_stage_module._truncate_identifier_bytes("short", 10) == "short"
    assert (
        gp_stage_module._truncate_identifier_bytes(
            "\u0430\u0431\u0432",
            5,
        )
        == "\u0430\u0431"
    )


def test_adapter_default_normalization_rejects_empty_values() -> None:
    adapter = get_backend_adapter("gp")

    assert (
        adapter_defaults_module.normalize_ch_columns_or_expression(
            adapter,
            " id ",
            "order_by",
        )
        == "id"
    )
    with pytest.raises(ValueError, match="must not be empty when provided"):
        adapter_defaults_module.normalize_ch_columns_or_expression(
            adapter,
            [],
            "order_by",
        )
    with pytest.raises(ValueError, match="order_by must not be empty"):
        adapter_defaults_module.normalize_ch_string(adapter, "  ", "order_by")


@pytest.mark.parametrize(
    ("function", "args", "kwargs"),
    [
        (
            adapter_defaults_module.build_show_tables_query,
            (object(), object(), None, None, None),
            {},
        ),
        (
            adapter_defaults_module.extract_table_ddl,
            (object(), "db", "schema.target"),
            {"read_sql": lambda db, sql: None},
        ),
        (
            adapter_defaults_module.build_drop_partitions_sqls,
            (object(), "schema.target", ["2026-01-01"]),
            {},
        ),
        (
            adapter_defaults_module.build_create_partition_sql,
            (object(), "schema.target"),
            {"name": "p1"},
        ),
        (
            adapter_defaults_module.build_vacuum_table_sql,
            (object(), "schema.target"),
            {},
        ),
    ],
)
def test_adapter_abstract_defaults_raise_not_implemented(
    function: Any,
    args: tuple[object, ...],
    kwargs: dict[str, object],
) -> None:
    with pytest.raises(NotImplementedError):
        function(*args, **kwargs)


def test_adapter_default_partition_options_reject_backend_specific_inputs() -> None:
    with pytest.raises(
        InvalidSqlInputError,
        match="gp_truncate=True",
    ):
        adapter_defaults_module.validate_drop_partitions_options(
            object(),
            partition_column=None,
            gp_truncate=True,
        )
    with pytest.raises(
        InvalidSqlInputError,
        match="trino_partition_column",
    ):
        adapter_defaults_module.validate_drop_partitions_options(
            object(),
            partition_column="dt",
            gp_truncate=False,
        )


def test_adapter_default_stage_discovery_and_qualification() -> None:
    class Cursor:
        def __init__(self) -> None:
            self.executed: list[tuple[str, tuple[str, str]]] = []
            self.closed = False

        def execute(self, sql: str, params: tuple[str, str]) -> None:
            self.executed.append((sql, params))

        def fetchall(self) -> list[tuple[str]]:
            return [("stage_one",), ("stage_two",)]

        def close(self) -> None:
            self.closed = True

    cursor = Cursor()
    connection = SimpleNamespace(cursor=lambda: cursor)
    adapter = get_backend_adapter("gp")

    assert adapter_defaults_module.query_transfer_stage_table_names(
        adapter,
        connection,
        connection_key="warehouse",
        transfer_staging_schema="stage-schema",
        table_pattern="target__stage__%",
    ) == ["stage_one", "stage_two"]
    assert cursor.executed[0][1] == ("stage-schema", "target__stage__%")
    assert cursor.closed is True
    assert (
        adapter_defaults_module.qualify_transfer_stage_table_name(
            adapter,
            "warehouse",
            "stage-schema",
            "1stage",
        )
        == '"stage-schema"."1stage"'
    )


def test_adapter_default_create_kwargs_include_partition_and_order() -> None:
    adapter = get_backend_adapter("trino")
    common_kwargs = {
        "gp_distributed_by_key": None,
        "partition_by": ["dt"],
        "order_by": ["id"],
        "ch_engine": "ReplicatedMergeTree",
        "ch_cluster": "{cluster}",
        "ch_sharding_key": "rand()",
        "ch_only_shard": False,
    }

    assert adapter_defaults_module.build_load_target_create_kwargs(
        adapter,
        **common_kwargs,
        write_mode="append",
        original_target_exists=True,
    ) == {
        "gp_distributed_by_key": None,
        "partition_by": ["dt"],
        "order_by": ["id"],
    }
    assert adapter_defaults_module.build_create_from_sql_target_create_kwargs(
        adapter,
        **common_kwargs,
        drop_target_if_exists=True,
        target_exists_before_drop=True,
    ) == {
        "gp_distributed_by_key": None,
        "partition_by": ["dt"],
        "order_by": ["id"],
    }


@pytest.mark.parametrize(
    "overrides",
    [
        {"ch_only_shard": True},
        {"ch_cluster": "analytics"},
        {"ch_sharding_key": "id"},
    ],
)
def test_adapter_default_rejects_clickhouse_only_create_options(
    overrides: dict[str, object],
) -> None:
    options: dict[str, Any] = {
        "option_owner": "to_db",
        "partition_by": None,
        "order_by": None,
        "ch_engine": "ReplicatedMergeTree",
        "ch_cluster": "{cluster}",
        "ch_sharding_key": "rand()",
        "ch_only_shard": False,
    }
    options.update(overrides)

    with pytest.raises(ValueError, match=r"can only be used.*type 'ch'"):
        adapter_defaults_module.validate_ch_create_table_options(
            get_backend_adapter("gp"),
            **options,
        )


def test_adapter_default_transfer_and_insert_policies() -> None:
    adapter = get_backend_adapter("gp")
    batch = pd.DataFrame({"id": [1]})
    error = RuntimeError("insert failed")

    assert (
        adapter_defaults_module.resolve_transfer_staging_mode(
            adapter,
            None,
            s3_transfer_staging_schema=None,
            s3_transfer_staging_location=None,
        )
        is None
    )
    with pytest.raises(ValueError, match="trino_mode must be one of"):
        adapter_defaults_module.resolve_transfer_staging_mode(
            adapter,
            "csv",
            s3_transfer_staging_schema=None,
            s3_transfer_staging_location=None,
        )
    assert adapter_defaults_module.normalize_insert_batch(adapter, batch) is batch
    assert adapter_defaults_module.normalize_insert_rows(adapter, [[1, "a"]]) == [(1, "a")]
    assert (
        adapter_defaults_module.should_wrap_insert_error_as_ambiguous(
            adapter,
            object(),
            error,
        )
        is True
    )
    assert adapter_defaults_module.should_refresh_connection_before_insert_retry(adapter) is False
    assert (
        adapter_defaults_module.wait_for_table_absence(
            adapter,
            object(),
            "target",
        )
        is None
    )
    assert (
        adapter_defaults_module.estimate_source_rows(
            adapter,
            object(),
            "SELECT 1",
        )
        is None
    )


def test_adapter_default_vacuum_restores_autocommit_after_failure() -> None:
    execute_error = RuntimeError("vacuum failed")

    class Cursor:
        def __init__(self) -> None:
            self.closed = False

        def execute(self, sql: str) -> None:
            assert sql == "VACUUM target"
            raise execute_error

        def close(self) -> None:
            self.closed = True

    cursor = Cursor()
    connection = SimpleNamespace(autocommit=False, cursor=lambda: cursor)
    adapter = SimpleNamespace(
        build_vacuum_table_sql=lambda table_name, **kwargs: f"VACUUM {table_name}"
    )

    with pytest.raises(RuntimeError, match="vacuum failed"):
        adapter_defaults_module.vacuum_table(adapter, connection, "target")

    assert connection.autocommit is False
    assert cursor.closed is True


def test_adapter_default_vacuum_supports_connection_without_autocommit() -> None:
    executed: list[str] = []
    cursor = SimpleNamespace(
        execute=executed.append,
        close=lambda: executed.append("closed"),
    )
    connection = SimpleNamespace(cursor=lambda: cursor)
    adapter = SimpleNamespace(
        build_vacuum_table_sql=lambda table_name, **kwargs: f"VACUUM {table_name}"
    )

    adapter_defaults_module.vacuum_table(adapter, connection, "target")

    assert executed == ["VACUUM target", "closed"]


def test_adapter_default_identifier_checks_empty_name() -> None:
    assert adapter_defaults_module._is_simple_identifier("") is False


class TrinoRecordingCursor:
    def __init__(self, *, fail_on: str | None = None) -> None:
        self.fail_on = fail_on
        self.executed: list[str] = []
        self.description = [("answer",)]
        self.closed = False

    def execute(self, sql: str, _params: Any = None) -> None:
        self.executed.append(sql)
        if self.fail_on and self.fail_on in sql:
            message = "trino query failed"
            raise RuntimeError(message)

    def fetchall(self) -> list[tuple[int]]:
        return [(7,)]

    def close(self) -> None:
        self.closed = True


def test_trino_adapter_schema_merge_and_upsert_guards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = get_backend_adapter("trino")
    with pytest.raises(ValueError, match="positive integer"):
        adapter.build_dataframe_batch_insert_sql("target", ["id"], row_count=0)
    expected = [SourceColumn("id", "bigint")]
    source_schema = importlib.import_module("analytics_toolkit.sql.backends.source_schema")
    monkeypatch.setattr(
        source_schema,
        "inspect_dbapi_source_schema",
        lambda *_args, **_kwargs: expected,
    )
    assert adapter.inspect_source_query_schema(object(), "SELECT id") == expected
    with pytest.raises(ValueError, match="partition_column and final_stage_table"):
        adapter.build_upsert_stage_sqls("target", "stage", columns=["id"], key_columns=["id"])
    with pytest.raises(ValueError, match="partition_column and final_stage_table"):
        adapter.build_upsert_stage_placeholder_sqls("target", "stage", key_columns=["id"])
    merge = adapter._build_merge_sql("target", "stage", columns=["id", "value"], key_columns=["id"])
    placeholder = adapter._build_merge_placeholder_sql("target", "stage", key_columns=["id"])
    null_safe = (
        'target_dst."id" = stage_src."id" OR (target_dst."id" IS NULL AND stage_src."id" IS NULL)'
    )
    assert null_safe in merge
    assert null_safe in placeholder


def test_greenplum_upsert_finalizes_every_incoming_stage() -> None:
    adapter = get_backend_adapter("gp")
    sqls = adapter.build_upsert_stage_sqls(
        "target",
        "stage_a",
        columns=["id", "value"],
        key_columns=["id"],
        incoming_stage_tables=["stage_a", "stage_b"],
    )

    assert len(sqls) == 4
    assert all("stage_a" in sql for sql in sqls[:2])
    assert all("stage_b" in sql for sql in sqls[2:])


def test_trino_adapter_execute_and_read_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = get_backend_adapter("trino")
    messages: list[str] = []
    monkeypatch.setattr(
        importlib.import_module("analytics_toolkit.general"),
        "time_print",
        lambda message, **_kwargs: messages.append(message),
    )
    cursor = TrinoRecordingCursor()
    adapter.execute_sql(
        SimpleNamespace(cursor=lambda: cursor),
        "SELECT 1; SELECT 2",
        print_queries=False,
        gp_break_query=False,
        gp_commit_each_statement=False,
        progress=False,
    )
    assert cursor.executed == ["SELECT 1", "SELECT 2"]
    assert cursor.closed is True
    failing = TrinoRecordingCursor(fail_on="SELECT 2")
    with pytest.raises(RuntimeError, match="trino query failed"):
        adapter.execute_sql(
            SimpleNamespace(cursor=lambda: failing),
            "SELECT 1; SELECT 2",
            print_queries=False,
            gp_break_query=False,
            gp_commit_each_statement=False,
            progress=False,
        )
    assert failing.closed is True
    assert "Failed SQL:\nSELECT 2" in messages
    read_cursor = TrinoRecordingCursor()
    result = adapter.execute_read_sql(
        SimpleNamespace(cursor=lambda: read_cursor),
        ["SET SESSION x = 1", "SELECT 7"],
        print_queries=False,
        gp_break_query=False,
        gp_commit_each_statement=False,
        progress=False,
    )
    assert result.to_dict("records") == [{"answer": 7}]
    assert read_cursor.closed is True
    broken_read = TrinoRecordingCursor(fail_on="SELECT broken")
    with pytest.raises(RuntimeError, match="trino query failed"):
        adapter.execute_read_sql(
            SimpleNamespace(cursor=lambda: broken_read),
            ["SELECT broken"],
            print_queries=False,
            gp_break_query=False,
            gp_commit_each_statement=False,
            progress=False,
        )
    assert broken_read.closed is True
    assert "Failed SQL:\nSELECT broken" in messages


def test_trino_adapter_insert_forwarding(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = get_backend_adapter("trino")
    calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
    monkeypatch.setattr(
        adapter,
        "_insert_dataframe_batch",
        lambda *args, **kwargs: calls.append(("frame", args, kwargs)),
    )
    monkeypatch.setattr(
        adapter,
        "_insert_rows",
        lambda *args, **kwargs: calls.append(("rows", args, kwargs)),
    )
    frame = pd.DataFrame({"id": [1]})
    common = {
        "target_column_types": {"id": "bigint"},
        "gp_insert_chunk_size": 99,
        "connection_type": "warehouse",
        "query_label": "load",
        "on_progress": None,
    }
    adapter.insert_dataframe_batch(object(), "target", frame, trino_insert_chunk_size=3, **common)
    adapter.insert_rows_batch(
        object(), "target", ["id"], [(1,)], trino_insert_chunk_size=4, **common
    )
    assert calls[0][2]["trino_insert_chunk_size"] == 3
    assert calls[1][2]["trino_insert_chunk_size"] == 4
    calls.clear()
    monkeypatch.setattr(
        importlib.import_module("analytics_toolkit.sql.backends.trino.insert"),
        "insert_rows",
        lambda *args, **kwargs: calls.append(("module", args, kwargs)),
    )
    adapter = trino_adapter_module.TrinoAdapter()
    adapter._insert_dataframe_batch(object(), "target", frame)
    assert calls[0][1][3] == pd.Index(["id"])
    assert calls[0][1][4] == [(1,)]


def test_trino_adapter_query_states_and_properties() -> None:
    adapter = get_backend_adapter("trino")
    assert "system.runtime.queries" in adapter.running_query_ids_sql()
    queries = adapter.show_queries_sqls(user="O'Reilly", states=["active", "finished", "failed"])
    assert [query["history"] for query in queries] == [False, True]
    assert "\"user\" = 'O''Reilly'" in queries[0]["sql"]
    assert "state in ('FINISHED', 'FAILED')" in queries[1]["sql"]
    assert adapter.show_queries_sqls(user=None, states=[]) == []
    with pytest.raises(ValueError, match="Unsupported Trino history state"):
        trino_adapter_module._trino_history_state("cancelled")
    properties = trino_adapter_module._build_trino_table_properties(
        partition_by="event_date", order_by=["id", "created_at"]
    )
    assert "partitioning = ARRAY['event_date']" in properties
    assert "sorted_by = ARRAY['id', 'created_at']" in properties
    with pytest.raises(ValueError, match="must not be empty"):
        trino_adapter_module._normalize_trino_property_entries([], "order_by")
    with pytest.raises(ValueError, match="duplicate"):
        trino_adapter_module._normalize_trino_property_entries(["id", "id"], "order_by")


@pytest.mark.parametrize(
    ("native_type", "expected"),
    [
        ("varbinary", "VARBINARY"),
        ("boolean", "BOOLEAN"),
        ("int8", "TINYINT"),
        ("int16", "SMALLINT"),
        ("integer", "INTEGER"),
        ("uint32", "BIGINT"),
        ("uint64", "DECIMAL(20, 0)"),
        ("int64", "BIGINT"),
        ("real", "REAL"),
        ("double", "DOUBLE"),
        ("numeric(12, 3)", "DECIMAL(12, 3)"),
        ("date", "DATE"),
        ("timestamp with time zone", "TIMESTAMP WITH TIME ZONE"),
        ("timestamp", "TIMESTAMP"),
        ("uuid", "UUID"),
        ("text", "VARCHAR"),
    ],
)
def test_trino_adapter_source_type_mapping(native_type: str, expected: str) -> None:
    assert (
        get_backend_adapter("trino").map_source_type_to_target(SourceColumn("value", native_type))
        == expected
    )


def test_trino_adapter_partition_template_validation() -> None:
    adapter = get_backend_adapter("trino")
    with pytest.raises(ValueError, match="requires upsert_partition_drop"):
        adapter.build_drop_upsert_partition_sqls(
            "target", partition_column="day", partition_values=[date(2026, 1, 2)]
        )
    with pytest.raises(ValueError, match="unsupported placeholder"):
        trino_adapter_module._validate_trino_partition_drop_template(
            "ALTER TABLE {table} DROP {unknown}"
        )
    with pytest.raises(ValueError, match="must contain placeholders"):
        trino_adapter_module._validate_trino_partition_drop_template(
            "ALTER TABLE {table} DROP {partition_column}"
        )
    template = "ALTER TABLE {table} DROP ({partition_column} = {partition_value})"
    assert (
        "<affected partition value>"
        in adapter.build_drop_upsert_partition_sqls(
            "target",
            partition_column="day",
            partition_values=None,
            trino_partition_drop_sql_template=template,
        )[0]
    )


def test_clickhouse_adapter_maintenance_delete_and_progress_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = get_backend_adapter("ch")
    client = RecordingClickHouseClient()
    adapter.truncate_table(client, "analytics.events", ch_cluster="cluster one")
    assert client.commands[-1][0] == (
        "TRUNCATE TABLE IF EXISTS analytics.events ON CLUSTER 'cluster one'"
    )
    with pytest.raises(UnsupportedConnectionTypeError, match="does not support ANALYZE"):
        adapter.analyze_table_sql("analytics.events")
    assert adapter.analyze_table(client, "analytics.events") is None

    delete_sql = adapter._build_delete_matching_stage_sql(
        "analytics.events",
        "analytics.events_stage",
        ["id", "group"],
        ch_cluster="{cluster}",
    )
    assert "tuple(isNull(`id`), ifNull(toString(`id`), '')" in delete_sql
    assert "SELECT tuple(isNull(`id`)" in delete_sql

    progress: list[int] = []
    frame = pd.DataFrame({"id": [1, 2]})
    client.insert_df = lambda **kwargs: None  # type: ignore[attr-defined]
    client.insert = lambda **kwargs: None  # type: ignore[attr-defined]
    adapter._insert_dataframe_batch(client, "events", frame, progress.append)
    adapter._insert_rows(
        client,
        "events",
        ["id"],
        [(1,), (2,), (3,)],
        {"id": "UInt64"},
        progress.append,
    )
    adapter._insert_rows(client, "events", ["id"], [(4,)], None)
    assert progress == [2, 3]


def test_clickhouse_adapter_execute_and_read_failures_report_last_sql(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = get_backend_adapter("ch")
    messages: list[str] = []
    monkeypatch.setattr(
        importlib.import_module("analytics_toolkit.general"),
        "time_print",
        lambda message, **_kwargs: messages.append(message),
    )

    command_error = RuntimeError("command failed")
    read_error = RuntimeError("read failed")

    class FailingClient:
        def command(self, sql: str) -> None:
            if sql == "SELECT 2":
                raise command_error

        def query_df(self, sql: str) -> pd.DataFrame:
            raise read_error

    with pytest.raises(RuntimeError, match="command failed"):
        adapter.execute_sql(
            FailingClient(),
            "SELECT 1; SELECT 2",
            print_queries=False,
            gp_break_query=False,
            gp_commit_each_statement=False,
            progress=False,
        )
    with pytest.raises(RuntimeError, match="read failed"):
        adapter.execute_read_sql(
            FailingClient(),
            ["SELECT broken"],
            print_queries=False,
            gp_break_query=False,
            gp_commit_each_statement=False,
            progress=False,
        )
    assert "Failed SQL:\nSELECT 2" in messages
    assert "Failed SQL:\nSELECT broken" in messages


@pytest.mark.parametrize(
    "case",
    [
        ("append", True, False, True, True),
        ("append", False, False, True, False),
        ("truncate_insert", True, True, True, True),
        ("truncate_insert", False, True, False, False),
        ("replace", True, True, True, False),
        ("truncate_insert", True, False, True, True),
        ("truncate_insert", False, False, False, False),
        ("replace", True, False, True, False),
    ],
)
def test_clickhouse_adapter_write_mode_matrix(
    monkeypatch: pytest.MonkeyPatch,
    case: tuple[str, bool, bool, bool, bool],
) -> None:
    write_mode, target_exists, only_shard, drop_missing, expected = case
    adapter = get_backend_adapter("ch")
    events: list[str] = []
    monkeypatch.setattr(adapter, "clear_table", lambda *args, **kwargs: events.append("clear"))
    monkeypatch.setattr(adapter, "drop_table", lambda *args, **kwargs: events.append("drop"))
    monkeypatch.setattr(
        ch_lifecycle_backend_module,
        "truncate_ch_distributed_table_pair",
        lambda *args, **kwargs: events.append("truncate_pair"),
    )
    monkeypatch.setattr(
        ch_lifecycle_backend_module,
        "drop_ch_distributed_table_pair",
        lambda *args, **kwargs: events.append("drop_pair"),
    )
    result = adapter.apply_target_write_mode(
        TargetWriteModeRequest(
            connection=object(),
            table_name="analytics.events",
            write_mode=write_mode,
            target_exists=target_exists,
            replace_existing_non_ch="clear",
            drop_missing_ch_truncate_target=drop_missing,
            ch_only_shard=only_shard,
        )
    )
    assert result is expected
    if write_mode == "replace":
        assert events == (["drop"] if only_shard else ["drop_pair"])


def test_clickhouse_adapter_stage_finalization_and_existing_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = get_backend_adapter("ch")
    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(
        adapter,
        "ensure_distributed_target_pair",
        lambda *args, **kwargs: calls.append(("ensure", kwargs.get("target_exists"))),
    )
    monkeypatch.setattr(
        adapter,
        "insert_from_table",
        lambda *args, **kwargs: calls.append(("insert", args[2])),
    )
    request = StageTargetTableRequest(
        connection=object(),
        target_table="events",
        sample_batch=pd.DataFrame({"id": [1]}),
        target_column_types={"id": "UInt64"},
        gp_distributed_by_key=None,
        partition_by=None,
        order_by=["id"],
        ch_engine="MergeTree",
        ch_cluster="cluster",
        ch_sharding_key="id",
        query_label=None,
        connection_key="ch",
    )
    assert adapter.ensure_stage_target_table(request) is True
    assert calls == [("ensure", False)]

    calls.clear()
    adapter.finalize_stage_table(
        StageFinalizationRequest(
            connection=object(),
            stage_table="events_stage",
            target_table="events",
            replace_target_table=False,
            target_exists=False,
            sample_batch=pd.DataFrame({"id": [1]}),
            target_column_types={"id": "UInt64"},
            write_mode="upsert",
        )
    )
    assert calls == [("ensure", False), ("insert", "events_stage")]

    metadata_adapter = ch_adapter_module.ClickHouseAdapter()
    monkeypatch.setattr(
        metadata_adapter,
        "get_table_column_types",
        lambda *args, **kwargs: {"stored_id": "UInt32"},
    )
    create_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        importlib.import_module("analytics_toolkit.sql.ddl.api"),
        "_create_sql_table_with_connection",
        lambda *args, **kwargs: create_calls.append(kwargs),
    )
    metadata_adapter.ensure_distributed_target_pair(
        object(),
        "events",
        pd.DataFrame({"id": [1]}),
        target_exists=True,
        target_column_types=None,
        insert_column_types=None,
        gp_distributed_by_key=None,
        partition_by=None,
        order_by=None,
        ch_engine="MergeTree",
        ch_cluster="cluster",
        ch_sharding_key="rand()",
        query_label=None,
        connection_key="ch",
    )
    assert create_calls[0]["table_schema"] == {"stored_id": "UInt32"}


class MinimalContractAdapter(BackendAdapter):
    backend = "minimal"
    display_name = "Minimal"
    sqlglot_dialect = "postgres"
    identifier_quote = '"'
    supports_transactions = False
    supports_analyze = True
    supports_distributed_tables = False
    truncate_semantics = "truncate"
    drop_semantics = "drop"
    create_semantics = "create"
    type_family = "test"


def test_base_adapter_metadata_abstract_and_value_contracts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = MinimalContractAdapter()
    assert adapter.name == "minimal"
    assert adapter.capability.display_name == "Minimal"

    executed: list[str] = []
    monkeypatch.setattr(
        adapter,
        "execute_command",
        lambda _connection, sql: executed.append(sql),
    )
    adapter.analyze_table(object(), "public.events", query_label="contract")
    assert executed
    assert "ANALYZE public.events" in executed[0]

    with pytest.raises(NotImplementedError):
        adapter.iter_source_batches(
            connection_key="source",
            connection_ref={"connection": object()},
            query="SELECT 1",
            get_batch_size=lambda: 1,
            retry_cnt=0,
            timeout_increment=0,
        )
    with pytest.raises(NotImplementedError):
        adapter.build_drop_upsert_partition_sqls(
            "target",
            partition_column="day",
            partition_values=None,
        )
    with pytest.raises(UnsupportedConnectionTypeError, match="does not support"):
        adapter.build_dataframe_batch_insert_sql("target", ["id"], row_count=1)

    with pytest.raises(ValueError, match="missing staged column"):
        adapter.column_types_for_columns({"id": "BIGINT"}, ["id", "value"])
    assert adapter.type_code_name(None, None, None) is None
    assert adapter.type_code_name(SimpleNamespace(type_name="custom"), None, None) == "custom"
    assert adapter.type_code_name(object(), None, None).startswith("<object object")
    with pytest.raises(ValueError, match="empty strings"):
        adapter.normalize_query_id("  ")
    assert adapter.normalize_query_id(7) == "7"
    with pytest.raises(ValueError, match="strings or integers"):
        adapter.normalize_query_id(True)
    assert "FROM (SELECT 1)" in adapter.build_insert_from_query_sql(
        "target", " SELECT 1; ", {"id": "BIGINT"}
    )


def test_base_adapter_write_stage_and_finalization_contracts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = MinimalContractAdapter()
    events: list[Any] = []
    monkeypatch.setattr(adapter, "clear_table", lambda *args, **kwargs: events.append("clear"))
    monkeypatch.setattr(adapter, "drop_table", lambda *args, **kwargs: events.append("drop"))

    def request(**values: Any) -> TargetWriteModeRequest:
        return TargetWriteModeRequest(
            connection=object(),
            table_name="target",
            write_mode=values.get("write_mode", "replace"),
            target_exists=values.get("target_exists", True),
            replace_existing_non_ch=values.get("policy", "clear"),
        )

    assert adapter.apply_target_write_mode(request(write_mode="append")) is True
    assert adapter.apply_target_write_mode(request(policy="drop")) is False
    with pytest.raises(ValueError, match="clear, drop"):
        adapter.apply_target_write_mode(request(policy="invalid"))
    assert events == ["drop"]

    creates: list[dict[str, Any]] = []
    monkeypatch.setattr(
        importlib.import_module("analytics_toolkit.sql.ddl.api"),
        "_create_sql_table_with_connection",
        lambda *args, **kwargs: creates.append(kwargs),
    )
    adapter.ensure_stage_target_table(
        StageTargetTableRequest(
            connection=object(),
            target_table="target",
            sample_batch=pd.DataFrame({"id": [1]}),
            target_column_types={"id": "BIGINT"},
            gp_distributed_by_key=None,
            partition_by=["day"],
            order_by=["id"],
            ch_engine="MergeTree",
            ch_cluster="cluster",
            ch_sharding_key="id",
            query_label=None,
            connection_key="minimal",
        )
    )
    assert creates[0]["partition_by"] == ["day"]
    assert creates[0]["order_by"] == ["id"]

    events.clear()
    monkeypatch.setattr(
        adapter,
        "ensure_stage_target_table",
        lambda _request: events.append("ensure") or True,
    )
    monkeypatch.setattr(
        adapter,
        "insert_from_table",
        lambda *args, **kwargs: events.append(("insert", args[2])),
    )
    for target_exists in (True, False):
        adapter.finalize_stage_table(
            StageFinalizationRequest(
                connection=object(),
                stage_table="stage",
                target_table="target",
                replace_target_table=False,
                target_exists=target_exists,
                sample_batch=pd.DataFrame({"id": [1]}),
            )
        )
    assert events == [("insert", "stage"), "ensure", ("insert", "stage")]


def test_clickhouse_adapter_upsert_partition_and_identifier_helpers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = get_backend_adapter("ch")
    monkeypatch.setattr(adapter, "ensure_distributed_target_pair", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        adapter, "fetch_upsert_partition_values", lambda *args, **kwargs: ["2026-01"]
    )
    monkeypatch.setattr(
        adapter, "build_upsert_stage_sqls", lambda *args, **kwargs: ["ALTER TABLE x"]
    )
    commands: list[str] = []
    monkeypatch.setattr(adapter, "execute_command", lambda _connection, sql: commands.append(sql))
    adapter.finalize_stage_table(
        StageFinalizationRequest(
            connection=object(),
            stage_table="events_stage",
            target_table="events",
            replace_target_table=False,
            target_exists=True,
            sample_batch=pd.DataFrame({"id": [1], "month": ["2026-01"]}),
            target_column_types={"id": "UInt64", "month": "String"},
            write_mode="upsert",
            upsert_partition_column="month",
        )
    )
    assert commands == ["ALTER TABLE x"]
    with pytest.raises(ValueError, match="upsert_partition_column is required"):
        adapter.finalize_stage_table(
            StageFinalizationRequest(
                connection=object(),
                stage_table="stage",
                target_table="events",
                replace_target_table=False,
                target_exists=True,
                sample_batch=pd.DataFrame({"id": [1]}),
                write_mode="upsert",
            )
        )
    assert "system.processes" in adapter.running_query_ids_sql()
    assert adapter.cancel_status(pd.DataFrame()) == (True, "submitted")
    assert ch_adapter_module.ch_cluster_clause(None) == ""
    with pytest.raises(ValueError, match="must not be empty"):
        ch_adapter_module.ch_cluster_clause("  ")
    assert ch_adapter_module.format_ch_cluster_name("`quoted`") == "`quoted`"
    assert ch_adapter_module.is_simple_identifier("") is False


def test_clickhouse_ddl_expression_cluster_and_uuid_edges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert ch_ddl_backend_module._normalize_ch_expression(" toYYYYMM(day) ", "x") == (
        "toYYYYMM(day)"
    )
    assert ch_ddl_backend_module._normalize_ch_expression(["day"], "x") == "`day`"
    with pytest.raises(ValueError, match="must not be empty when provided"):
        ch_ddl_backend_module._normalize_ch_expression([], "x")
    with pytest.raises(ValueError, match="duplicate"):
        ch_ddl_backend_module._normalize_ch_expression(["day", "day"], "x")
    with pytest.raises(ValueError, match="must not be empty"):
        ch_ddl_backend_module._normalize_non_empty_string("  ", "x")

    assert ch_ddl_backend_module._format_ch_cluster_name("  ") == ""
    assert ch_ddl_backend_module._format_ch_cluster_name("`core-prod`") == "`core-prod`"
    assert ch_ddl_backend_module._format_ch_cluster_name("core-prod") == "'core-prod'"
    assert ch_ddl_backend_module._is_simple_identifier("") is False
    assert ch_ddl_backend_module._is_simple_identifier("9core") is False
    assert ch_ddl_backend_module._is_simple_identifier("_core9") is True

    commands: list[str] = []
    monkeypatch.setattr(
        get_backend_adapter("ch"),
        "execute_command",
        lambda _connection, sql: commands.append(sql),
    )
    ch_ddl_backend_module._execute_ch_command(object(), "SELECT 1")
    assert commands == ["SELECT 1"]

    unchanged = [
        "CREATE TABLE x ON CLUSTER core\n(id UInt64) ENGINE = ReplicatedMergeTree",
        "CREATE TABLE x\n(id UInt64) ENGINE = MergeTree",
        "CREATE TABLE x\nUUID 'fixed'\n(id UInt64) ENGINE = ReplicatedMergeTree",
        "CREATE TABLE x ENGINE = ReplicatedMergeTree",
    ]
    for sql in unchanged:
        assert ch_ddl_backend_module.add_explicit_ch_uuid_to_local_replicated_create(sql) == sql
    monkeypatch.setattr(ch_ddl_backend_module.uuid, "uuid4", lambda: "fixed-uuid")
    rewritten = ch_ddl_backend_module.add_explicit_ch_uuid_to_local_replicated_create(
        "CREATE TABLE x\n(id UInt64) ENGINE = ReplicatedMergeTree"
    )
    assert "UUID 'fixed-uuid'" in rewritten


def test_clickhouse_insert_legacy_collections_types_and_null_edges() -> None:
    class LegacyFrame:
        def applymap(self, function: Any) -> pd.DataFrame:
            return pd.DataFrame({"value": [function(Decimal("1.25")), function(None)]})

    normalized = ch_insert_backend_module.normalize_batch(
        LegacyFrame()  # type: ignore[arg-type]
    )
    assert normalized.to_dict("records") == [{"value": 1.25}, {"value": None}]
    assert ch_insert_backend_module.normalize_scalar(
        [Decimal("1.5"), (Decimal("2.5"),), {Decimal("3.5"): Decimal("4.5")}]
    ) == [1.5, (2.5,), {3.5: 4.5}]
    assert ch_insert_backend_module.column_type_names(["a"], None) is None
    assert ch_insert_backend_module.column_type_names(
        ["a", "b"], {"a": "UInt8", "b": "String"}
    ) == ["UInt8", "String"]
    with pytest.raises(ValueError, match="Missing explicit SQL type for column 'b'"):
        ch_insert_backend_module.column_type_names(["a", "b"], {"a": "UInt8"})
    assert ch_insert_backend_module.normalize_row(([1, 2], None)) == ([1, 2], None)
    assert ch_insert_backend_module.normalize_typed_row(
        ["id", "amount", "payload"],
        [1.0, 1.25, {"x": "я"}],
        {"id": "Nullable(Int64)", "amount": "Decimal(10,2)", "payload": "String"},
    ) == (1, Decimal("1.25"), '{"x":"я"}')
    assert ch_insert_backend_module.normalize_typed_row(
        ["raw", "mutable"],
        [memoryview(b"\x00\xff"), bytearray(b"\x01\x80")],
        {"raw": "Nullable(String)", "mutable": "LowCardinality(String)"},
    ) == (b"\x00\xff", b"\x01\x80")
    assert ch_insert_backend_module.normalize_typed_row(["value"], ["plain"], None) == (
        "plain",
    )
    assert ch_insert_backend_module.normalize_rows(
        ["value"],
        [[True]],
        {"value": "Bool"},
    ) == [(True,)]
    inserted: list[dict[str, Any]] = []
    native = SimpleNamespace(
        is_native_transport=True,
        insert=lambda **kwargs: inserted.append(kwargs),
    )
    ch_insert_backend_module.insert_dataframe_batch(
        native,
        "events",
        pd.DataFrame({"id": [1.0, None]}),
        {"id": "Nullable(Int64)"},
    )
    assert inserted[0]["data"] == [(1,), (None,)]
    http_inserts: list[dict[str, Any]] = []
    http = SimpleNamespace(
        is_native_transport=False,
        insert_df=lambda **kwargs: http_inserts.append(kwargs),
    )
    ch_insert_backend_module.insert_dataframe_batch(
        http,
        "events",
        pd.DataFrame(
            {
                "raw": [memoryview(b"\x00\xff")],
                "mutable": [bytearray(b"\x01\x80")],
            }
        ),
        {"raw": "String", "mutable": "Nullable(String)"},
    )
    assert http_inserts[0]["df"].to_dict("records") == [
        {"raw": b"\x00\xff", "mutable": b"\x01\x80"}
    ]
    assert ch_insert_backend_module._is_null_like(None) is True
    assert ch_insert_backend_module._is_null_like([1, 2]) is False


def test_clickhouse_lifecycle_drop_modes_and_wait_routing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert len(ch_lifecycle_backend_module.build_drop_ch_table_sqls("db.t", None)) == 1
    executed: list[str] = []
    monkeypatch.setattr(
        ch_lifecycle_backend_module,
        "_execute_ch_sqls",
        lambda _connection, sqls: executed.extend(sqls),
    )
    local_waits: list[str] = []
    cluster_waits: list[str] = []
    monkeypatch.setattr(
        ch_lifecycle_backend_module,
        "_wait_for_ch_table_absence",
        lambda _connection, table, **_kwargs: local_waits.append(table),
    )
    monkeypatch.setattr(
        ch_lifecycle_backend_module,
        "_wait_for_ch_table_absence_on_cluster",
        lambda _connection, table, **_kwargs: cluster_waits.append(table),
    )
    ch_lifecycle_backend_module.drop_ch_table(
        object(), "db.local", ch_cluster=None, wait_for_absence=True
    )
    ch_lifecycle_backend_module.drop_ch_table(
        object(), "db.clustered", ch_cluster="core", wait_for_absence=True
    )
    assert local_waits == ["db.local"]
    assert cluster_waits == ["db.clustered"]
    ch_lifecycle_backend_module.truncate_ch_distributed_table_pair(object(), "db.t")
    ch_lifecycle_backend_module.create_ch_distributed_table_pair(
        object(), table_name="db.t", joined_columns="id UInt64", wait_for_table=False
    )
    waited: list[str] = []
    monkeypatch.setattr(
        ch_lifecycle_backend_module,
        "_wait_for_ch_distributed_table_pair",
        lambda _connection, table, **_kwargs: waited.append(table),
    )
    ch_lifecycle_backend_module.create_ch_distributed_table_pair(
        object(), table_name="db.t", joined_columns="id UInt64", wait_for_table=True
    )
    assert waited == ["db.t"]


def test_clickhouse_lifecycle_timeout_requirements(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ch_lifecycle_backend_module, "_execute_ch_sqls", lambda *_: None)
    monkeypatch.setattr(
        ch_lifecycle_backend_module,
        "_wait_for_ch_distributed_table_pair_absence",
        lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError("lagging")),
    )
    with pytest.raises(TimeoutError, match="lagging"):
        ch_lifecycle_backend_module.drop_ch_distributed_table_pair(
            object(), "db.t", wait_for_absence=True, ch_retry_per_host_drops=False
        )
    with pytest.raises(TimeoutError, match="non-null ch_cluster"):
        ch_lifecycle_backend_module.drop_ch_distributed_table_pair(
            object(),
            "db.t",
            ch_cluster=None,
            wait_for_absence=True,
            ch_retry_per_host_drops=True,
        )


def test_clickhouse_lifecycle_host_selection_and_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pair = ch_lifecycle_backend_module.ch_distributed_table_pair("db.t")
    assert (
        ch_lifecycle_backend_module._select_ch_hosts_for_local_drop(
            object(), pair, ch_cluster="core", configured_hosts=[]
        )
        == []
    )
    monkeypatch.setattr(
        ch_lifecycle_backend_module,
        "_resolve_ch_cluster_name_for_wait",
        lambda _connection, cluster: cluster,
    )
    monkeypatch.setattr(
        ch_lifecycle_backend_module,
        "_query_ch_cluster_table_rows",
        lambda *args, **kwargs: [],
    )
    assert ch_lifecycle_backend_module._select_ch_hosts_for_local_drop(
        object(), pair, ch_cluster="core", configured_hosts=["a", "b"]
    ) == ["a", "b"]
    monkeypatch.setattr(
        ch_lifecycle_backend_module,
        "_query_ch_cluster_table_rows",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("query failed")),
    )
    assert ch_lifecycle_backend_module._select_ch_hosts_for_local_drop(
        object(), pair, ch_cluster="core", configured_hosts=["a"]
    ) == ["a"]

    class Result:
        def __init__(self) -> None:
            self.result_rows = [(), ("  ",), (" host-a ",)]

    class Connection:
        def query(self, _sql: str) -> Result:
            return Result()

    assert ch_lifecycle_backend_module._query_ch_configured_cluster_hosts(Connection(), "core") == [
        "host-a"
    ]
    monkeypatch.setattr(
        ch_lifecycle_backend_module,
        "_query_ch_configured_cluster_hosts",
        lambda *_: [],
    )
    with pytest.raises(TimeoutError, match="could not find any configured"):
        ch_lifecycle_backend_module._drop_ch_distributed_table_pair_on_cluster_hosts(
            object(),
            pair,
            ch_cluster="core",
            query_label=None,
            per_host_drop_workers=1,
            per_host_connection_factory=lambda _host: object(),
        )


def test_clickhouse_lifecycle_per_host_error_and_close_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pair = ch_lifecycle_backend_module.ch_distributed_table_pair("db.t")
    factory_error = RuntimeError("boom")

    def fail_factory(_host: str) -> Any:
        raise factory_error

    assert "boom" in ch_lifecycle_backend_module._drop_ch_distributed_table_pair_on_host(
        "host-a",
        pair=pair,
        query_label=None,
        per_host_connection_factory=fail_factory,
    )

    close_error = RuntimeError("close failed")

    class ClosingConnection:
        def close(self) -> None:
            raise close_error

    monkeypatch.setattr(ch_lifecycle_backend_module, "_execute_ch_sqls", lambda *_: None)
    assert (
        ch_lifecycle_backend_module._drop_ch_distributed_table_pair_on_host(
            "host-a",
            pair=pair,
            query_label=None,
            per_host_connection_factory=lambda _host: None,
        )
        is None
    )
    assert (
        ch_lifecycle_backend_module._drop_ch_distributed_table_pair_on_host(
            "host-a",
            pair=pair,
            query_label=None,
            per_host_connection_factory=lambda _host: object(),
        )
        is None
    )
    with pytest.raises(RuntimeError, match="close failed"):
        ch_lifecycle_backend_module._drop_ch_distributed_table_pair_on_host(
            "host-a",
            pair=pair,
            query_label=None,
            per_host_connection_factory=lambda _host: ClosingConnection(),
        )


def test_clickhouse_target_explicit_type_validation() -> None:
    batch = pd.DataFrame({"a": [1], "b": [2]})
    adapter = SimpleNamespace(infer_dataframe_column_type=lambda _series: "UInt64")
    with pytest.raises(ValueError, match="Missing explicit SQL type for column 'b'"):
        ch_target_create_backend_module.expected_create_table_column_types(
            adapter,
            batch,
            {"a": "UInt8"},
            ch_distributed_table=True,
            ch_only_shard=False,
        )
    with pytest.raises(ValueError, match="must not be empty"):
        ch_target_create_backend_module.expected_create_table_column_types(
            adapter,
            pd.DataFrame({"a": [1]}),
            {"a": "  "},
            ch_distributed_table=True,
            ch_only_shard=False,
        )


def test_clickhouse_upsert_requirements_and_placeholder_sqls() -> None:
    adapter = SimpleNamespace(
        build_preserved_target_rows_insert_sql=lambda *args, **kwargs: "preserved",
        build_incoming_rows_insert_sql=lambda *args, **kwargs: "incoming",
        build_drop_upsert_partition_sqls=lambda *args, **kwargs: ["drop"],
        build_insert_from_stage_placeholder_sql=lambda *args, **kwargs: "final",
    )
    with pytest.raises(ValueError, match="are required"):
        ch_upsert_backend_module.build_upsert_stage_sqls(
            adapter,
            "target",
            "stage",
            columns=["id"],
            key_columns=["id"],
        )
    with pytest.raises(ValueError, match="are required"):
        ch_upsert_backend_module.build_upsert_stage_placeholder_sqls(
            adapter, "target", "stage", key_columns=["id"]
        )
    assert ch_upsert_backend_module.build_upsert_stage_placeholder_sqls(
        adapter,
        "target",
        "stage",
        key_columns=["id"],
        upsert_partition_column="month",
        final_stage_table="final_stage",
    ) == ["preserved", "incoming", "drop", "final"]


def test_clickhouse_wait_eventual_and_timeout_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Result:
        def __init__(self, rows: list[tuple[object, ...]]) -> None:
            self.result_rows = rows

    class SequenceConnection:
        def __init__(self, rows: list[list[tuple[object, ...]]]) -> None:
            self.rows = iter(rows)

        def query(self, _sql: str) -> Result:
            return Result(next(self.rows))

    ticks = iter([0.0, 0.1, 0.2, 0.3])
    sleeps: list[float] = []
    monkeypatch.setattr(ch_backend_wait_module.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(ch_backend_wait_module.time, "sleep", sleeps.append)
    ch_backend_wait_module._wait_for_ch_table(
        SequenceConnection([[(0,)], [(1,)]]),
        "db.t",
        timeout_seconds=1,
        poll_interval_seconds=0.25,
    )
    assert sleeps == [0.25]

    ticks = iter([0.0, 0.1, 0.2, 0.3])
    sleeps.clear()
    ch_backend_wait_module._wait_for_ch_table_absence(
        SequenceConnection([[(1,)], [(0,)]]),
        "db.t",
        timeout_seconds=1,
        poll_interval_seconds=0.5,
    )
    assert sleeps == [0.5]

    monkeypatch.setattr(
        ch_backend_wait_module,
        "_query_ch_expected_cluster_hosts",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("schema query")),
    )
    monkeypatch.setattr(ch_backend_wait_module.time, "monotonic", lambda: 1.0)
    monkeypatch.setattr(
        ch_backend_wait_module,
        "_describe_ch_cluster_schema_mismatch",
        lambda *args, **kwargs: "details",
    )
    with pytest.raises(TimeoutError, match="details") as exc_info:
        ch_backend_wait_module._wait_for_ch_table_schema_on_cluster(
            object(),
            "db.t",
            expected_column_types={"id": "UInt64"},
            ch_cluster="core",
            timeout_seconds=0,
            poll_interval_seconds=0,
        )
    assert isinstance(exc_info.value.__cause__, RuntimeError)


def test_clickhouse_lifecycle_concurrent_host_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pair = ch_lifecycle_backend_module.ch_distributed_table_pair("db.t")
    monkeypatch.setattr(
        ch_lifecycle_backend_module,
        "_query_ch_configured_cluster_hosts",
        lambda *_: ["host-a"],
    )
    monkeypatch.setattr(
        ch_lifecycle_backend_module,
        "_select_ch_hosts_for_local_drop",
        lambda *args, **kwargs: ["host-a"],
    )
    monkeypatch.setattr(
        ch_lifecycle_backend_module,
        "_drop_ch_distributed_table_pair_on_host",
        lambda *args, **kwargs: "host-a: failed",
    )
    with pytest.raises(TimeoutError, match="host-a: failed"):
        ch_lifecycle_backend_module._drop_ch_distributed_table_pair_on_cluster_hosts(
            object(),
            pair,
            ch_cluster="core",
            query_label=None,
            per_host_drop_workers=1,
            per_host_connection_factory=lambda _host: object(),
        )
    monkeypatch.setattr(
        ch_lifecycle_backend_module,
        "_drop_ch_distributed_table_pair_on_host",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("worker crashed")),
    )
    with pytest.raises(TimeoutError, match="worker crashed"):
        ch_lifecycle_backend_module._drop_ch_distributed_table_pair_on_cluster_hosts(
            object(),
            pair,
            ch_cluster="core",
            query_label=None,
            per_host_drop_workers=1,
            per_host_connection_factory=lambda _host: object(),
        )


def test_clickhouse_wait_schema_diagnostics_and_macro_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed = (
        [("extra", "String", 1)] + [(f"c{i}", "Wrong", 1) for i in range(7)] + [("malformed",)]
    )
    monkeypatch.setattr(
        ch_backend_wait_module,
        "_query_ch_rows",
        lambda *_: observed,
    )
    details = ch_backend_wait_module._describe_ch_cluster_schema_mismatch(
        object(),
        "db.t",
        expected_column_types={f"c{i}": "UInt64" for i in range(7)},
        ch_cluster="core",
        expected_hosts=2,
    )
    assert "Schema mismatch details" in details
    assert details.endswith("...")

    monkeypatch.setattr(
        ch_backend_wait_module,
        "_query_ch_rows",
        lambda *_: [("amount", "Decimal(18, 4)", 1)],
    )
    equivalent_details = ch_backend_wait_module._describe_ch_cluster_schema_mismatch(
        object(),
        "db.t",
        expected_column_types={"amount": "Decimal(18,4)"},
        ch_cluster="core",
        expected_hosts=1,
    )
    assert equivalent_details == ""

    class EmptyMacroConnection:
        def query(self, _sql: str) -> Any:
            return SimpleNamespace(result_rows=[])

    with pytest.raises(ValueError, match="Could not resolve"):
        ch_backend_wait_module._resolve_ch_cluster_name_for_wait(
            EmptyMacroConnection(), "{cluster}"
        )

    macro_error = RuntimeError("macro failed")

    class FailingMacroConnection:
        def query(self, _sql: str) -> Any:
            raise macro_error

    with pytest.raises(ValueError, match="Could not resolve") as exc_info:
        ch_backend_wait_module._resolve_ch_cluster_name_for_wait(
            FailingMacroConnection(), "{cluster}"
        )
    assert isinstance(exc_info.value.__cause__, RuntimeError)


def test_clickhouse_wait_cluster_absence_wrapper_and_plain_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        ch_backend_wait_module,
        "_wait_for_ch_tables_absence_on_cluster",
        lambda _connection, names, **_kwargs: calls.append(names),
    )
    ch_backend_wait_module._wait_for_ch_table_absence_on_cluster(
        object(), "db.t", ch_cluster="core"
    )
    assert calls == [["db.t"]]

    monkeypatch.setattr(
        ch_backend_wait_module,
        "_query_ch_expected_cluster_hosts",
        lambda *args, **kwargs: 1,
    )
    monkeypatch.setattr(ch_backend_wait_module, "_query_ch_count", lambda *args: 0)
    monkeypatch.setattr(ch_backend_wait_module.time, "monotonic", lambda: 1.0)
    monkeypatch.setattr(
        ch_backend_wait_module,
        "_describe_ch_cluster_schema_mismatch",
        lambda *args, **kwargs: "",
    )
    with pytest.raises(TimeoutError, match="did not match expected") as exc_info:
        ch_backend_wait_module._wait_for_ch_table_schema_on_cluster(
            object(),
            "db.t",
            expected_column_types={"id": "UInt64"},
            ch_cluster="core",
            timeout_seconds=0,
            poll_interval_seconds=0,
        )
    assert exc_info.value.__cause__ is None
