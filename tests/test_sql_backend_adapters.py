from __future__ import annotations

import importlib
import inspect
import threading

import pandas as pd
import pytest

from analytics_toolkit.sql.backend_adapters import BACKEND_ADAPTERS, get_backend_adapter
from analytics_toolkit.sql.backends import BACKEND_REGISTRY, get_backend, get_backend_names
from analytics_toolkit.sql.backends.base import BackendAdapter
from analytics_toolkit.sql.backends.registry import get_backend_capability
from analytics_toolkit.sql.connection.errors import (
    SqlConfigError,
    UnsupportedConnectionTypeError,
)
from tests.sql_fakes import FakeClickHouseResult, FakeDbapiConnection


sql_module = importlib.import_module("analytics_toolkit.sql")
read_sql_module = importlib.import_module("analytics_toolkit.sql.dml.io.read_sql")
execute_sql_module = importlib.import_module("analytics_toolkit.sql.dml.io.execute_sql")
execute_read_module = importlib.import_module(
    "analytics_toolkit.sql.dml.io.execute_read"
)
load_sql_table_module = importlib.import_module(
    "analytics_toolkit.sql.dml.load.load_sql_table"
)
table_ops_module = importlib.import_module("analytics_toolkit.sql.dml.table.api")
table_basic_ops_module = importlib.import_module(
    "analytics_toolkit.sql.dml.table._basic_ops"
)
ch_lifecycle_module = importlib.import_module("analytics_toolkit.sql.clickhouse.lifecycle")
ch_wait_module = importlib.import_module("analytics_toolkit.sql.clickhouse.wait")


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


def test_clickhouse_wait_helpers_are_lifecycle_owned_with_ddl_shims() -> None:
    assert (
        ch_lifecycle_module._wait_for_ch_distributed_table_pair
        is ch_wait_module._wait_for_ch_distributed_table_pair
    )
    assert (
        ch_lifecycle_module._wait_for_ch_distributed_table_pair_absence
        is ch_wait_module._wait_for_ch_distributed_table_pair_absence
    )
    assert (
        ch_lifecycle_module._query_ch_cluster_table_rows
        is ch_wait_module._query_ch_cluster_table_rows
    )
    assert "from .wait import" in inspect.getsource(ch_lifecycle_module)


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
    ) == (
        'INSERT INTO schema.stage ("id", "name") VALUES (?, ?), (?, ?)'
    )
    assert get_backend_adapter("gp").build_stage_duplicate_keys_sql(
        "schema.stage",
        ["id", "dt"],
    ) == (
        'SELECT 1 FROM schema.stage GROUP BY "id", "dt" '
        "HAVING COUNT(*) > 1 LIMIT 1"
    )
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
    }
    inherited_contract_methods = {
        "build_insert_from_stage_sql",
        "build_insert_from_stage_placeholder_sql",
        "build_create_from_sql_target_create_kwargs",
        "build_load_target_create_kwargs",
        "column_types_for_columns",
        "refine_stage_column_types_from_rows",
        "should_ensure_load_target_table",
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
    assert ch_adapter.build_create_from_sql_target_create_kwargs(
        gp_distributed_by_key=None,
        partition_by=None,
        order_by=None,
        ch_engine="MergeTree",
        ch_cluster="cluster",
        ch_sharding_key="id",
        ch_only_shard=True,
        drop_target_if_exists=True,
        target_exists_before_drop=True,
    )["ch_distributed_table"] is False


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


def test_legacy_backend_imports_resolve_to_canonical_objects() -> None:
    legacy_module = importlib.import_module("analytics_toolkit.sql._backend_adapters")
    public_compat_module = importlib.import_module(
        "analytics_toolkit.sql.backend_adapters"
    )

    assert legacy_module.BACKEND_ADAPTERS is BACKEND_REGISTRY
    assert public_compat_module.BACKEND_ADAPTERS is BACKEND_REGISTRY
    for backend_name in get_backend_names():
        assert legacy_module.get_backend_adapter(backend_name) is BACKEND_REGISTRY[backend_name]
        assert (
            public_compat_module.get_backend_adapter(backend_name)
            is BACKEND_REGISTRY[backend_name]
        )


def test_backend_compatibility_class_exports_are_lazy() -> None:
    from analytics_toolkit.sql.backends import (
        BackendAdapter as CanonicalBackendAdapter,
    )
    from analytics_toolkit.sql.backends import (
        BackendCapability as CanonicalBackendCapability,
    )
    from analytics_toolkit.sql.backends import (
        ClickHouseAdapter as CanonicalClickHouseAdapter,
    )
    from analytics_toolkit.sql.backends import (
        DbApiBackendAdapter as CanonicalDbApiBackendAdapter,
    )
    from analytics_toolkit.sql.backends import (
        GreenplumAdapter as CanonicalGreenplumAdapter,
    )
    from analytics_toolkit.sql.backends import TrinoAdapter as CanonicalTrinoAdapter

    class_exports = [
        (
            "analytics_toolkit.sql.backend_adapters",
            "BackendAdapter",
            CanonicalBackendAdapter,
        ),
        (
            "analytics_toolkit.sql.backend_adapters",
            "BackendCapability",
            CanonicalBackendCapability,
        ),
        (
            "analytics_toolkit.sql.backend_adapters",
            "ClickHouseAdapter",
            CanonicalClickHouseAdapter,
        ),
        (
            "analytics_toolkit.sql.backend_adapters",
            "DbApiBackendAdapter",
            CanonicalDbApiBackendAdapter,
        ),
        (
            "analytics_toolkit.sql.backend_adapters",
            "GreenplumAdapter",
            CanonicalGreenplumAdapter,
        ),
        (
            "analytics_toolkit.sql.backend_adapters",
            "TrinoAdapter",
            CanonicalTrinoAdapter,
        ),
        (
            "analytics_toolkit.sql._backend_adapters",
            "BackendAdapter",
            CanonicalBackendAdapter,
        ),
        (
            "analytics_toolkit.sql._backend_adapters",
            "BackendCapability",
            CanonicalBackendCapability,
        ),
        (
            "analytics_toolkit.sql._backend_adapters.base",
            "BackendAdapter",
            CanonicalBackendAdapter,
        ),
        (
            "analytics_toolkit.sql._backend_adapters.base",
            "BackendCapability",
            CanonicalBackendCapability,
        ),
        (
            "analytics_toolkit.sql._backend_adapters.dbapi",
            "DbApiBackendAdapter",
            CanonicalDbApiBackendAdapter,
        ),
        (
            "analytics_toolkit.sql._backend_adapters.gp",
            "GreenplumAdapter",
            CanonicalGreenplumAdapter,
        ),
        (
            "analytics_toolkit.sql._backend_adapters.trino",
            "TrinoAdapter",
            CanonicalTrinoAdapter,
        ),
        (
            "analytics_toolkit.sql._backend_adapters.clickhouse",
            "ClickHouseAdapter",
            CanonicalClickHouseAdapter,
        ),
        (
            "analytics_toolkit.sql.core.capabilities",
            "BackendCapability",
            CanonicalBackendCapability,
        ),
    ]

    for module_name, export_name, canonical_object in class_exports:
        module = importlib.import_module(module_name)
        assert getattr(module, export_name) is canonical_object
        assert export_name not in vars(module)


def test_sql_backend_dispatch_uses_callable_registries() -> None:
    dispatch_functions = [
        read_sql_module._read_backend,
        execute_sql_module._execute_backend,
        execute_read_module._execute_read_backend,
        load_sql_table_module._insert_batch_backend,
        load_sql_table_module._insert_rows_backend,
    ]
    for function in dispatch_functions:
        assert "globals()[" not in inspect.getsource(function)

    expected_backends = set(get_backend_names())

    assert set(read_sql_module._READ_BACKENDS) == expected_backends
    assert all(callable(callback) for callback in read_sql_module._READ_BACKENDS.values())
    assert set(execute_sql_module._EXECUTE_BACKENDS) == expected_backends
    assert all(
        callable(callback)
        for callback in execute_sql_module._EXECUTE_BACKENDS.values()
    )
    assert set(execute_read_module._EXECUTE_READ_BACKENDS) == expected_backends
    assert all(
        callable(callback)
        for callback in execute_read_module._EXECUTE_READ_BACKENDS.values()
    )
    assert set(load_sql_table_module._BATCH_INSERT_BACKENDS) == expected_backends
    assert all(
        callable(callback)
        for callback in load_sql_table_module._BATCH_INSERT_BACKENDS.values()
    )
    assert set(load_sql_table_module._ROW_INSERT_BACKENDS) == expected_backends
    assert all(
        callable(callback)
        for callback in load_sql_table_module._ROW_INSERT_BACKENDS.values()
    )


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
        read_dbapi_query=lambda connection, query: pytest.fail(
            "ClickHouse should use query_df"
        ),
    )

    pd.testing.assert_frame_equal(ch_result, pd.DataFrame({"value": [2]}))
    assert ch_client.queries == ["select value"]


def test_backend_adapters_execute_operations_like_existing_table_ops() -> None:
    gp_connection = FakeDbapiConnection(rows=[(5,)])
    get_backend_adapter("gp").clear_table(gp_connection, "schema.target")
    assert gp_connection.executed == ["TRUNCATE TABLE schema.target"]
    assert gp_connection.commit_calls == 1

    trino_connection = FakeDbapiConnection(rows=[(7,)])
    assert get_backend_adapter("trino").count_table_rows(
        trino_connection,
        "schema.target",
    ) == 7
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
    assert get_backend_adapter("ch").stage_keys_overlap_target(
        ch_client,
        "db.stage",
        "db.target",
        ["id"],
    ) is False
    assert ch_client.queries[-1] == (
        "SELECT 1 FROM db.stage AS stage_src "
        "INNER JOIN db.target AS target_dst ON "
        "(stage_src.`id` = target_dst.`id` "
        "OR (stage_src.`id` IS NULL AND target_dst.`id` IS NULL)) "
        "LIMIT 1"
    )


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
    assert get_backend_adapter("gp").insert_from_query(
        gp_connection,
        "schema.target",
        "select id from source",
        {"id": "BIGINT"},
    ) == 4
    assert gp_connection.commit_calls == 1

    ch_client = RecordingClickHouseClient()
    assert get_backend_adapter("ch").insert_from_query(
        ch_client,
        "db.target",
        "select id from source",
        {"id": "Nullable(Int64)"},
    ) == 3
    assert ch_client.commands[-1][0] == (
        "INSERT INTO db.target (`id`) "
        "SELECT CAST(`id` AS Nullable(Int64)) AS `id` "
        "FROM (select id from source) AS source_query"
    )
