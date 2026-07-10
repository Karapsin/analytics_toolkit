from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

import pandas as pd
import pytest
from analytics_toolkit.sql.connection.errors import InvalidSqlInputError

if TYPE_CHECKING:
    from collections.abc import Callable


basic_ops = importlib.import_module("analytics_toolkit.sql.dml.table._basic_ops")
write_modes = importlib.import_module("analytics_toolkit.sql.dml.table.write_modes")


class RecordingAdapter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.results: dict[str, Any] = {
            "table_exists": True,
            "clear_table_sqls": ["CLEAR target"],
            "drop_table_sql": "DROP target",
            "build_drop_tables_sqls": ["DROP shard", "DROP distributed"],
            "analyze_table_sql": "ANALYZE target",
            "get_table_column_types": {"id": "bigint"},
            "insert_from_query": 4,
            "build_insert_from_query_sql": "INSERT query",
            "build_insert_from_table_sql": "INSERT table",
            "count_table_rows": 5,
            "count_table_rows_sql": "SELECT count(*)",
            "_build_typed_insert_select_sql": "INSERT typed",
            "cast_select_expression": "CAST(id AS bigint)",
            "apply_target_write_mode": True,
            "build_upsert_stage_sqls": ["UPSERT one", "UPSERT two"],
            "build_upsert_stage_placeholder_sqls": ["UPSERT placeholder"],
            "fetch_upsert_partition_values": ["2026-01-01"],
            "_build_merge_sql": "MERGE",
            "_build_merge_placeholder_sql": "MERGE placeholder",
            "_build_delete_matching_stage_sql": "DELETE matching",
            "_build_normalized_key_tuple": "tuple(id)",
            "build_insert_from_stage_sql": "INSERT stage",
            "build_explicit_insert_from_stage_sql": "INSERT explicit",
            "build_insert_from_stage_placeholder_sql": "INSERT stage placeholder",
            "column_types_for_columns": {"id": "bigint"},
            "ensure_stage_target_table": True,
        }

    def __getattr__(self, name: str) -> Callable[..., Any]:
        def method(*args: Any, **kwargs: Any) -> Any:
            self.calls.append((name, args, kwargs))
            return self.results.get(name)

        return method


@pytest.fixture
def adapter(monkeypatch: pytest.MonkeyPatch) -> RecordingAdapter:
    recording_adapter = RecordingAdapter()
    monkeypatch.setattr(basic_ops, "resolve_connection_backend", lambda value: value)
    monkeypatch.setattr(basic_ops, "get_backend_adapter", lambda _backend: recording_adapter)
    monkeypatch.setattr(write_modes, "resolve_connection_backend", lambda value: value)
    monkeypatch.setattr(write_modes, "get_backend_adapter", lambda _backend: recording_adapter)
    monkeypatch.setattr(write_modes, "time_print", lambda *_args, **_kwargs: None)
    return recording_adapter


def test_basic_table_facade_forwards_public_operations(adapter: RecordingAdapter) -> None:
    connection = object()

    assert basic_ops.table_exists("gp", connection, "schema.target") is True
    assert basic_ops.table_exists("gp", connection, "schema.target", connection_key="alias") is True
    assert basic_ops.build_clear_table_sqls("gp", "schema.target", "q") == ["CLEAR target"]
    assert (
        basic_ops.build_drop_table_sql("gp", "schema.target", if_exists=False, query_label="q")
        == "DROP target"
    )
    assert basic_ops.build_drop_ch_distributed_table_pair_sqls(
        "db.target", ch_cluster="cluster", if_exists=False
    ) == ["DROP shard", "DROP distributed"]
    assert basic_ops.build_analyze_table_sql("gp", "schema.target", "q") == ("ANALYZE target")
    assert basic_ops.get_trino_table_column_types(connection, "catalog.schema.target", "alias") == {
        "id": "bigint"
    }
    assert basic_ops.get_table_column_types("gp", connection, "schema.target") == {"id": "bigint"}
    assert basic_ops.get_table_column_types(
        "gp", connection, "schema.target", connection_key="alias"
    ) == {"id": "bigint"}
    assert basic_ops._get_gp_table_column_types(connection, "schema.target") == {"id": "bigint"}
    assert basic_ops._get_ch_table_column_types(connection, "db.target") == {"id": "bigint"}

    basic_ops.insert_from_table(
        "gp",
        connection,
        "schema.target",
        "schema.source",
        {"id": "bigint"},
        "q",
    )
    assert (
        basic_ops.insert_from_query(
            "gp", connection, "schema.target", "SELECT 1", {"id": "bigint"}, "q"
        )
        == 4
    )
    assert basic_ops.count_table_rows("gp", connection, "schema.target", "q") == 5
    assert basic_ops.build_count_table_rows_sql("gp", "schema.target", "q") == ("SELECT count(*)")

    names = [call[0] for call in adapter.calls]
    assert names.count("table_exists") == 2
    assert "insert_from_table" in names
    assert "insert_from_query" in names
    assert "count_table_rows" in names


def test_basic_table_facade_builds_insert_and_cast_sql(adapter: RecordingAdapter) -> None:
    column_types = {"id": "bigint"}

    assert (
        basic_ops.build_insert_from_query_sql("gp", "target", "SELECT id FROM source", column_types)
        == "INSERT query"
    )
    assert (
        basic_ops.build_insert_from_table_sql("gp", "target", "source", column_types)
        == "INSERT table"
    )
    assert (
        basic_ops._build_insert_from_table_sql("gp", "target", "source", column_types)
        == "INSERT table"
    )
    assert (
        basic_ops._build_typed_insert_select_sql("gp", "target", "SELECT id", column_types)
        == "INSERT typed"
    )
    assert basic_ops._cast_select_expression("gp", "id", "bigint") == ("CAST(id AS bigint)")


@pytest.mark.parametrize(
    ("mapping", "expected"),
    [
        ({"rowcount": None, "rows": "4"}, 4),
        ({"rows": True}, None),
        ({"rows": -1}, None),
        ({"rows": "bad"}, None),
        ({}, None),
    ],
)
def test_basic_table_row_count_mapping_coercion(
    mapping: dict[str, Any],
    expected: int | None,
) -> None:
    assert basic_ops._extract_row_count_from_mapping(mapping) == expected


def test_basic_table_identifier_and_backend_compatibility_helpers(
    adapter: RecordingAdapter,
) -> None:
    connection = object()

    assert basic_ops._split_gp_table_name("schema.target") == ("schema", "target")
    assert basic_ops.split_trino_table_name("catalog.schema.target") == (
        "catalog",
        "schema",
        "target",
    )
    assert basic_ops.quote_qualified_table_name("schema.target", "gp") == ('"schema"."target"')
    with pytest.raises(InvalidSqlInputError, match="non-empty"):
        basic_ops.quote_qualified_table_name("", "gp")
    with pytest.raises(InvalidSqlInputError, match="non-empty"):
        basic_ops.quote_qualified_table_name("a.b.c.d", "trino")

    basic_ops._truncate_ch_table(connection, "db.target", "cluster", "q")
    basic_ops._execute_ch_command(connection, "OPTIMIZE TABLE target")
    assert basic_ops._gp_table_exists(connection, "schema.target") is True
    assert basic_ops._trino_table_exists(connection, "catalog.schema.target", "alias") is True
    assert basic_ops._ch_table_exists(connection, "db.target") is True

    assert " ON CLUSTER " in basic_ops._ch_cluster_clause("cluster")
    assert basic_ops._ch_cluster_clause(None) == ""
    assert basic_ops._format_ch_cluster_name("cluster") == "cluster"
    assert basic_ops._is_simple_identifier("valid_name") is True
    assert basic_ops._is_simple_identifier("not valid") is False
    assert "truncate_table" in [call[0] for call in adapter.calls]
    assert "execute_command" in [call[0] for call in adapter.calls]


def test_clear_target_table_returns_plan_or_executes(adapter: RecordingAdapter) -> None:
    connection = object()

    plan = write_modes.clear_target_table(
        "gp", connection, "schema.target", query_label="q", dry_run=True
    )
    result = write_modes.clear_target_table("gp", connection, "schema.target")

    assert plan.sqls == ["CLEAR target"]
    assert plan.statements[0].phase == "clear_target"
    assert plan.metadata.statement_count == 1
    assert result is None
    assert adapter.calls[-1][0] == "clear_table"


@pytest.mark.parametrize("connection_label", [None, "display alias"])
def test_apply_target_write_mode_builds_adapter_request(
    adapter: RecordingAdapter,
    connection_label: str | None,
) -> None:
    connection = object()

    result = write_modes.apply_target_write_mode(
        "gp",
        connection,
        "schema.target",
        write_mode="replace",
        target_exists=True,
        replace_existing_non_ch="drop",
        connection_label=connection_label,
        query_label="q",
        connection_key="alias",
    )

    request = adapter.calls[-1][1][0]
    assert result is True
    assert request.connection is connection
    assert request.table_name == "schema.target"
    assert request.connection_label == (connection_label or "gp")
    assert request.connection_key == "alias"


@pytest.mark.parametrize(
    ("columns", "key_columns", "message"),
    [([], ["id"], "columns are required"), (["id"], [], "key_columns are required")],
)
def test_build_upsert_stage_sqls_requires_columns_and_keys(
    columns: list[str],
    key_columns: list[str],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        write_modes.build_upsert_stage_sqls(
            "gp",
            "target",
            "stage",
            columns=columns,
            key_columns=key_columns,
        )


def test_build_upsert_stage_sqls_and_placeholder_delegate(adapter: RecordingAdapter) -> None:
    assert write_modes.build_upsert_stage_sqls(
        "gp",
        "target",
        "stage",
        columns=["id"],
        key_columns=["id"],
        column_types={"id": "bigint"},
    ) == ["UPSERT one", "UPSERT two"]
    assert write_modes.build_upsert_stage_placeholder_sqls(
        "gp", "target", "stage", key_columns=["id"]
    ) == ["UPSERT placeholder"]

    with pytest.raises(ValueError, match="key_columns are required"):
        write_modes.build_upsert_stage_placeholder_sqls("gp", "target", "stage", key_columns=[])


@pytest.mark.parametrize("partition_column", [None, "partition_date"])
def test_upsert_stage_table_fetches_optional_partitions_and_executes_sqls(
    adapter: RecordingAdapter,
    partition_column: str | None,
) -> None:
    connection = object()

    write_modes.upsert_stage_table(
        "gp",
        connection,
        "target",
        "stage",
        columns=["id", "partition_date"],
        key_columns=["id"],
        upsert_partition_column=partition_column,
    )

    names = [call[0] for call in adapter.calls]
    assert names.count("execute_command") == 2
    assert ("fetch_upsert_partition_values" in names) is (partition_column is not None)


def test_write_mode_compatibility_sql_helpers_delegate(adapter: RecordingAdapter) -> None:
    assert (
        write_modes._build_trino_merge_sql("target", "stage", columns=["id"], key_columns=["id"])
        == "MERGE"
    )
    assert (
        write_modes._build_trino_merge_placeholder_sql("target", "stage", key_columns=["id"])
        == "MERGE placeholder"
    )
    assert (
        write_modes._build_gp_delete_matching_stage_sql("target", "stage", ["id"])
        == "DELETE matching"
    )
    assert (
        write_modes._build_ch_delete_matching_stage_sql(
            "target", "stage", ["id"], ch_cluster="cluster"
        )
        == "DELETE matching"
    )
    assert write_modes._build_ch_normalized_key_tuple(["id"]) == "tuple(id)"
    assert (
        write_modes._build_insert_from_stage_sql(
            "gp",
            "target",
            "stage",
            columns=["id"],
            column_types={"id": "bigint"},
            query_label="q",
        )
        == "INSERT stage"
    )
    assert (
        write_modes._build_explicit_insert_from_stage_sql(
            "gp",
            "target",
            "stage",
            columns=["id"],
            column_types={"id": "bigint"},
        )
        == "INSERT explicit"
    )
    assert (
        write_modes._build_insert_from_stage_placeholder_sql(
            "gp", "target", "stage", query_label="q"
        )
        == "INSERT stage placeholder"
    )
    assert write_modes._column_types_for_columns({"id": "bigint", "other": "text"}, ["id"]) == {
        "id": "bigint"
    }


def test_finalize_stage_table_builds_full_request(adapter: RecordingAdapter) -> None:
    connection = object()
    batch = pd.DataFrame({"id": [1]})

    write_modes.finalize_stage_table(
        "gp",
        connection,
        "stage",
        "target",
        replace_target_table=True,
        target_exists=True,
        sample_batch=batch,
        target_column_types={"id": "bigint"},
        insert_column_types={"id": "bigint"},
        write_mode="upsert",
        key_columns=["id"],
        connection_key="alias",
        final_upsert_stage_table="aggregate_stage",
        incoming_stage_tables=["stage_1", "stage_2"],
    )

    request = adapter.calls[-1][1][0]
    assert request.connection is connection
    assert request.sample_batch is batch
    assert request.final_upsert_stage_table == "aggregate_stage"
    assert request.incoming_stage_tables == ["stage_1", "stage_2"]


def test_stage_target_and_distributed_pair_helpers_build_requests(
    adapter: RecordingAdapter,
) -> None:
    connection = object()
    batch = pd.DataFrame({"id": [1]})

    assert (
        write_modes._ensure_stage_target_table(
            backend="gp",
            connection=connection,
            target_table="target",
            sample_batch=batch,
            target_column_types={"id": "bigint"},
            gp_distributed_by_key=["id"],
            partition_by=None,
            order_by=None,
            ch_engine="ReplicatedMergeTree",
            ch_cluster="cluster",
            ch_sharding_key="rand()",
            query_label="q",
            connection_key="alias",
        )
        is True
    )
    request = adapter.calls[-1][1][0]
    assert request.target_table == "target"
    assert request.connection_key == "alias"

    write_modes._ensure_ch_distributed_target_pair(
        "ch",
        connection,
        "db.target",
        batch,
        target_exists=True,
        target_column_types={"id": "Int64"},
        insert_column_types={"id": "Int64"},
        gp_distributed_by_key=None,
        partition_by="toYYYYMM(date)",
        order_by=["id"],
        ch_engine="ReplicatedMergeTree",
        ch_cluster="cluster",
        ch_sharding_key="id",
        query_label="q",
        connection_key="alias",
        ch_replace_table=True,
        ch_only_shard=True,
    )
    assert adapter.calls[-1][0] == "ensure_distributed_target_pair"
    assert adapter.calls[-1][2]["ch_replace_table"] is True
    assert adapter.calls[-1][2]["ch_only_shard"] is True
