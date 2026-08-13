from __future__ import annotations

import builtins
import importlib
import sys
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pandas as pd
import pytest
from analytics_toolkit.sql.connection.errors import InvalidSqlInputError
from sqlglot import exp

ch_metadata = importlib.import_module("analytics_toolkit.sql.backends.ch.metadata")
ch_operations = importlib.import_module("analytics_toolkit.sql.backends.ch.operations")
ch_wait = importlib.import_module("analytics_toolkit.sql.backends.ch.wait")
gp_adapter_module = importlib.import_module("analytics_toolkit.sql.backends.gp.adapter")
gp_ddl = importlib.import_module("analytics_toolkit.sql.backends.gp.ddl")
gp_insert = importlib.import_module("analytics_toolkit.sql.backends.gp.insert")
gp_operations = importlib.import_module("analytics_toolkit.sql.backends.gp.operations")
trino_operations = importlib.import_module("analytics_toolkit.sql.backends.trino.operations")
trino_parquet = importlib.import_module("analytics_toolkit.sql.backends.trino.parquet_stage")


class QueryResult:
    def __init__(self, rows: list[tuple[Any, ...]] | None = None) -> None:
        self.result_rows = rows


class RecordingCursor:
    def __init__(
        self,
        rows: list[tuple[Any, ...]] | None = None,
        *,
        fail_on: str | None = None,
    ) -> None:
        self.rows = rows or []
        self.fail_on = fail_on
        self.executed: list[tuple[str, Any]] = []
        self.closed = False
        self.description = [("value",)]

    def __enter__(self) -> RecordingCursor:  # noqa: PYI034
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def execute(self, sql: str, params: Any = None) -> None:
        self.executed.append((sql, params))
        if self.fail_on is not None and self.fail_on in sql:
            message = "query failed"
            raise RuntimeError(message)

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self.rows

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.rows[0] if self.rows else None

    def close(self) -> None:
        self.closed = True


class RecordingConnection:
    def __init__(self, cursor: RecordingCursor | None = None) -> None:
        self.cursor_instance = cursor or RecordingCursor()
        self.commits = 0
        self.rollbacks = 0

    def cursor(self) -> RecordingCursor:
        return self.cursor_instance

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


class RoutingClickHouseConnection:
    def __init__(self, route: Any) -> None:
        self.route = route
        self.queries: list[str] = []

    def query(self, sql: str) -> QueryResult:
        self.queries.append(sql)
        value = self.route(sql)
        if isinstance(value, Exception):
            raise value
        return QueryResult(value)


def test_gp_execute_values_import_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Any, str, Any, int]] = []

    def fake_execute_values(cursor: Any, sql: str, rows: Any, *, page_size: int) -> str:
        calls.append((cursor, sql, rows, page_size))
        return "inserted"

    monkeypatch.setitem(
        sys.modules,
        "psycopg2.extras",
        SimpleNamespace(execute_values=fake_execute_values),
    )
    cursor = object()
    assert gp_insert.execute_values(cursor, "INSERT", [(1,)], 10) == "inserted"
    assert calls == [(cursor, "INSERT", [(1,)], 10)]

    real_import = builtins.__import__

    def reject_psycopg2(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "psycopg2.extras":
            message = "missing"
            raise ImportError(message)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", reject_psycopg2)
    with pytest.raises(ImportError, match="required for Greenplum"):
        gp_insert.execute_values(cursor, "INSERT", [(1,)], 10)


def test_gp_insert_normalization_and_dataframe_delegation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uuid_value = UUID(int=1)
    batch = pd.DataFrame(
        {
            "value": [1.0, float("nan")],
            "text": ["x", None],
            "uuid_value": [uuid_value, None],
        }
    )
    normalized = gp_insert.normalize_insert_batch(object(), batch)
    assert normalized.iloc[0].tolist() == [1.0, "x", str(uuid_value)]
    assert normalized.iloc[1].tolist() == [None, None, None]
    assert gp_insert.normalize_insert_rows(object(), [[pd.NA, uuid_value]]) == [
        (None, str(uuid_value))
    ]
    json_value = gp_insert.normalize_insert_rows(object(), [[{"nested": [1, 2]}]])[0][0]
    assert json_value.adapted == {"nested": [1, 2]}
    assert gp_insert._is_null_like([1, 2]) is False

    captured: dict[str, Any] = {}

    def fake_insert_rows(*args: Any, **kwargs: Any) -> None:
        captured["args"] = args
        captured["kwargs"] = kwargs

    monkeypatch.setattr(gp_insert, "insert_rows", fake_insert_rows)
    gp_insert.insert_dataframe_batch(
        object(),
        object(),
        "public.target",
        pd.DataFrame({"id": [1, 2]}),
        gp_insert_chunk_size=2,
        query_label="load",
    )
    assert captured["args"][2:5] == (
        "public.target",
        pd.Index(["id"]),
        [(1,), (2,)],
    )
    assert captured["kwargs"]["gp_insert_chunk_size"] == 2


def test_gp_insert_rows_chunks_callbacks_and_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = SimpleNamespace(
        build_dataframe_batch_insert_sql=lambda *_args, **_kwargs: "INSERT VALUES %s"
    )
    connection = RecordingConnection()
    pages: list[tuple[list[tuple[int, ...]], int]] = []
    progress: list[int] = []
    successes: list[tuple[float, int]] = []
    sizes = iter([2, 1])

    def fake_execute_values(
        _cursor: Any,
        _sql: str,
        rows: list[tuple[int, ...]],
        *,
        page_size: int,
    ) -> None:
        pages.append((rows, page_size))

    monkeypatch.setattr(gp_insert, "execute_values", fake_execute_values)
    gp_insert.insert_rows(
        adapter,
        connection,
        "target",
        ["id"],
        [[1], [2], [3]],
        page_size_getter=lambda: next(sizes),
        on_progress=progress.append,
        on_page_success=lambda duration, count: successes.append((duration, count)),
    )

    assert pages == [([(1,), (2,)], 2), ([(3,)], 1)]
    assert progress == [2, 1]
    assert [count for _, count in successes] == [2, 1]
    assert connection.commits == 1
    assert connection.cursor_instance.closed is True

    untouched = RecordingConnection()
    gp_insert.insert_rows(adapter, untouched, "target", ["id"], [])
    assert untouched.cursor_instance.closed is False


def test_gp_insert_rows_rolls_back_and_validates_chunk_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = SimpleNamespace(
        build_dataframe_batch_insert_sql=lambda *_args, **_kwargs: "INSERT VALUES %s"
    )
    connection = RecordingConnection()
    monkeypatch.setattr(
        gp_insert,
        "execute_values",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("insert failed")),
    )

    with pytest.raises(RuntimeError, match="insert failed"):
        gp_insert.insert_rows(adapter, connection, "target", ["id"], [[1]])
    assert connection.rollbacks == 1
    assert connection.cursor_instance.closed is True
    assert gp_insert.get_insert_chunk_size(None) == gp_insert.DEFAULT_GP_INSERT_CHUNK_SIZE
    with pytest.raises(ValueError, match="positive integer"):
        gp_insert.get_insert_chunk_size(0)


def test_greenplum_adapter_execution_and_type_branches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = gp_adapter_module.GreenplumAdapter()
    monkeypatch.setattr(
        importlib.import_module("analytics_toolkit.general"),
        "time_print",
        lambda *_args, **_kwargs: None,
    )

    assert adapter.build_vacuum_table_sql("public.events", verbose=False) == (
        'VACUUM "public"."events"'
    )
    assert adapter.build_vacuum_table_sql(
        "public.events",
        analyze=True,
        full=True,
    ).startswith("VACUUM (FULL, VERBOSE, ANALYZE)")
    assert adapter.planned_execute_statements("SELECT 1; SELECT 2", gp_break_query=True) == [
        "SELECT 1",
        "SELECT 2",
    ]

    connection = RecordingConnection()
    adapter.execute_sql(
        connection,
        "SELECT 1; SELECT 2",
        print_queries=False,
        gp_break_query=True,
        gp_commit_each_statement=True,
        progress=False,
    )
    assert [sql for sql, _ in connection.cursor_instance.executed] == [
        "SELECT 1",
        "SELECT 2",
    ]
    assert connection.commits == 2

    assert adapter.type_code_name(None, None, None) is None
    assert adapter.type_code_name(1700, 12, 3) == "numeric(12,3)"
    assert adapter.type_code_name(99999, None, None) == "99999"
    assert adapter.type_code_name("custom", None, None) == "custom"
    assert "pid as query_id" in adapter.running_query_ids_sql()
    with pytest.raises(ValueError, match="backend PIDs"):
        adapter.normalize_query_id(True)
    with pytest.raises(ValueError, match="backend PIDs"):
        adapter.normalize_query_id("not-a-pid")
    assert adapter.cancel_status(pd.DataFrame({"cancelled": [False]})) == (
        False,
        "not_cancelled",
    )


def test_greenplum_execute_loops_read_cleanup_and_dataframe_delegate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = gp_adapter_module.GreenplumAdapter()
    messages: list[str] = []
    monkeypatch.setattr(
        importlib.import_module("analytics_toolkit.general"),
        "time_print",
        lambda message, **_kwargs: messages.append(message),
    )

    connection = RecordingConnection()
    adapter.execute_sql(
        connection,
        "SELECT 1; SELECT 2",
        print_queries=False,
        gp_break_query=True,
        gp_commit_each_statement=False,
        progress=False,
    )
    assert connection.commits == 1

    failed_cursor = RecordingCursor(fail_on="SET broken")
    failed_connection = RecordingConnection(failed_cursor)
    with pytest.raises(RuntimeError, match="query failed"):
        adapter.execute_read_sql(
            failed_connection,
            ["SET broken", "SELECT 1"],
            print_queries=False,
            gp_break_query=False,
            gp_commit_each_statement=False,
            progress=False,
        )
    assert failed_cursor.closed is True
    assert "Failed SQL:\nSELECT 1" in messages

    calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        adapter,
        "_insert_rows",
        lambda *args, **kwargs: calls.append((*args, kwargs)),
    )
    adapter._insert_dataframe_batch(
        object(),
        "target",
        pd.DataFrame({"id": [1], "value": [None]}),
        gp_insert_chunk_size=3,
        query_label="batch",
        on_progress=None,
    )
    assert list(calls[0][2]) == ["id", "value"]
    assert calls[0][3] == [(1, None)]


def test_greenplum_adapter_insert_wrappers(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = gp_adapter_module.GreenplumAdapter()
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
    adapter.insert_dataframe_batch(
        object(),
        "target",
        frame,
        target_column_types=None,
        trino_insert_chunk_size=None,
        gp_insert_chunk_size=4,
        connection_type="gp",
        query_label="batch",
        on_progress=None,
    )
    adapter.insert_rows_batch(
        object(),
        "target",
        ["id"],
        [[1]],
        target_column_types=None,
        trino_insert_chunk_size=None,
        gp_insert_chunk_size=4,
        connection_type="gp",
        query_label="rows",
        on_progress=None,
    )
    assert [call[0] for call in calls] == ["frame", "rows"]
    assert adapter.normalize_insert_batch(frame).to_dict("list") == {"id": [1]}
    assert adapter.normalize_insert_rows([[pd.NA]]) == [(None,)]


@pytest.mark.parametrize(
    ("data_type", "udt_name", "precision", "scale", "expected"),
    [
        ("numeric", "numeric", 7, None, "NUMERIC(7)"),
        ("character varying", "varchar", None, None, "VARCHAR"),
        ("timestamp without time zone", "timestamp", None, None, "TIMESTAMP"),
        ("timestamp with time zone", "timestamptz", None, None, "TIMESTAMP WITH TIME ZONE"),
        ("integer", "int4", None, None, "INTEGER"),
        ("bigint", "int8", None, None, "BIGINT"),
        ("smallint", "int2", None, None, "SMALLINT"),
        ("boolean", "bool", None, None, "BOOLEAN"),
        ("date", "date", None, None, "DATE"),
        ("text", "text", None, None, "TEXT"),
        ("ARRAY", "_int4", None, None, "_INT4"),
    ],
)
def test_format_gp_information_schema_type_branches(
    data_type: str,
    udt_name: str,
    precision: int | None,
    scale: int | None,
    expected: str,
) -> None:
    assert (
        gp_adapter_module.format_gp_information_schema_type(
            data_type,
            udt_name,
            precision,
            scale,
        )
        == expected
    )


@pytest.mark.parametrize(
    ("kind", "source_type", "precision", "scale", "expected"),
    [
        ("integer", "smallint", None, None, "SMALLINT"),
        ("integer", "uint32", None, None, "BIGINT"),
        ("integer", "uint64", None, None, "NUMERIC(20, 0)"),
        ("integer", "int64", None, None, "BIGINT"),
        ("float", "float32", None, None, "REAL"),
        ("float", "float64", None, None, "DOUBLE PRECISION"),
        ("date", "date", None, None, "DATE"),
        ("timestamp", "timestamptz", None, None, "TIMESTAMP WITH TIME ZONE"),
        ("timestamp", "timestamp", None, None, "TIMESTAMP"),
        ("unknown", "object", None, None, "TEXT"),
    ],
)
def test_map_to_gp_type_branches(
    kind: str,
    source_type: str,
    precision: int | None,
    scale: int | None,
    expected: str,
) -> None:
    assert gp_adapter_module._map_to_gp_type(kind, source_type, precision, scale) == expected


def test_greenplum_partition_column_validation() -> None:
    assert gp_adapter_module._normalize_gp_partition_column(["event_date"]) == "event_date"
    with pytest.raises(ValueError, match="exactly one"):
        gp_adapter_module._normalize_gp_partition_column(["a", "b"])


def test_gp_ddl_catalog_reconstruction() -> None:
    def read_sql(_connection_key: str, query: str) -> pd.DataFrame:  # noqa: PLR0911
        if "FROM pg_catalog.pg_class AS c" in query:
            return pd.DataFrame(
                [
                    {
                        "oid": "42",
                        "schema_name": "reporting",
                        "relation_name": "events",
                        "reloptions": "{appendonly=true,orientation=column}",
                        "table_comment": "table note",
                    }
                ]
            )
        if "FROM pg_catalog.pg_attribute AS a" in query:
            return pd.DataFrame(
                [
                    {
                        "attnum": 1,
                        "column_name": "id",
                        "formatted_type": "bigint",
                        "default_expr": "1",
                        "is_not_null": True,
                        "column_comment": "identifier",
                    },
                    {
                        "attnum": 2,
                        "column_name": "payload",
                        "formatted_type": "text",
                        "default_expr": None,
                        "is_not_null": False,
                        "column_comment": None,
                    },
                ]
            )
        if "FROM pg_catalog.pg_constraint" in query:
            return pd.DataFrame(
                [
                    {
                        "constraint_name": "events_pk",
                        "constraint_def": "PRIMARY KEY (id)",
                    },
                    {"constraint_name": "ignored", "constraint_def": None},
                ]
            )
        if "FROM pg_catalog.pg_inherits" in query:
            return pd.DataFrame([{"parent_schema": "base", "parent_table": "parent"}])
        if "FROM pg_catalog.pg_index" in query:
            return pd.DataFrame([{"index_def": "CREATE INDEX events_payload_idx;"}])
        if "has_partkeydef" in query:
            return pd.DataFrame([{"has_partkeydef": True, "has_partition_def": False}])
        if "pg_get_partkeydef" in query:
            return pd.DataFrame([{"partition_def": "RANGE (id)"}])
        if "gp_distribution_policy" in query:
            return pd.DataFrame([{"policy_type": "p", "attrnums": "{1}"}])
        raise AssertionError(query)

    ddl = gp_ddl.extract_greenplum_catalog_ddl(
        "warehouse",
        "reporting.events",
        read_sql=read_sql,
    )
    assert 'CREATE TABLE "reporting"."events"' in ddl
    assert 'CONSTRAINT "events_pk" PRIMARY KEY (id)' in ddl
    assert 'INHERITS ("base"."parent")' in ddl
    assert "PARTITION BY RANGE (id)" in ddl
    assert 'DISTRIBUTED BY ("id")' in ddl
    assert "COMMENT ON TABLE" in ddl
    assert "COMMENT ON COLUMN" in ddl


def test_gp_ddl_missing_columns_and_partition_fallbacks() -> None:
    relation = pd.DataFrame([{"oid": "1", "schema_name": "public", "relation_name": "empty"}])

    def read_empty_columns(_connection_key: str, query: str) -> pd.DataFrame:
        if "FROM pg_catalog.pg_class AS c" in query:
            return relation
        if "FROM pg_catalog.pg_attribute AS a" in query:
            return pd.DataFrame()
        raise AssertionError(query)

    with pytest.raises(ValueError, match="No columns"):
        gp_ddl.extract_greenplum_catalog_ddl(
            "gp",
            "public.empty",
            read_sql=read_empty_columns,
        )

    assert (
        gp_ddl.read_gp_partition_clause(
            "gp",
            "1",
            read_sql=lambda *_args: pd.DataFrame(),
        )
        == ""
    )

    def legacy_partition(_connection_key: str, query: str) -> pd.DataFrame:
        if "has_partkeydef" in query:
            return pd.DataFrame([{"has_partkeydef": False, "has_partition_def": True}])
        return pd.DataFrame([{"partition_def": "PARTITION BY LIST (region);"}])

    assert (
        gp_ddl.read_gp_partition_clause(
            "gp",
            "1",
            read_sql=legacy_partition,
        )
        == "PARTITION BY LIST (region)"
    )
    assert gp_ddl.format_gp_partition_clause(" ; ") == ""


def test_gp_ddl_minimal_catalog_and_empty_helper_edges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read_partition_clause = gp_ddl.read_gp_partition_clause
    relation = pd.DataFrame(
        [
            {
                "oid": "1",
                "schema_name": "public",
                "relation_name": "events",
                "reloptions": None,
                "table_comment": None,
            }
        ]
    )
    columns = pd.DataFrame(
        [
            {
                "column_name": "id",
                "formatted_type": "integer",
                "default_expr": None,
                "is_not_null": False,
                "column_comment": None,
                "attnum": 1,
            }
        ]
    )

    def read_sql(_connection_key: str, query: str) -> pd.DataFrame:
        if "FROM pg_catalog.pg_class AS c" in query:
            return relation
        if "FROM pg_catalog.pg_attribute AS a" in query:
            return columns
        if any(
            marker in query
            for marker in (
                "FROM pg_catalog.pg_constraint",
                "FROM pg_catalog.pg_inherits",
                "FROM pg_catalog.pg_index",
            )
        ):
            return pd.DataFrame()
        raise AssertionError(query)

    monkeypatch.setattr(gp_ddl, "read_gp_partition_clause", lambda *args, **kwargs: "")
    monkeypatch.setattr(gp_ddl, "read_gp_distribution_clause", lambda *args, **kwargs: "")
    ddl = gp_ddl.extract_greenplum_catalog_ddl("gp", "public.events", read_sql=read_sql)
    assert ddl == 'CREATE TABLE "public"."events" (\n    "id" integer\n);'

    assert (
        read_partition_clause(
            "gp",
            "1",
            read_sql=lambda *_args: pd.DataFrame(
                [{"has_partkeydef": False, "has_partition_def": False}]
            ),
        )
        == ""
    )
    assert gp_ddl.format_gp_partition_clause(None) == ""
    with pytest.raises(ValueError, match="No DDL"):
        gp_ddl.first_result_value(pd.DataFrame(), "events")
    with pytest.raises(ValueError, match="No DDL"):
        gp_ddl.first_result_value(pd.DataFrame({"ddl": [pd.NA]}), "events")
    assert gp_ddl.parse_pg_array_text("   ") == []
    assert gp_ddl.parse_attrnums(None) == []


def test_gp_ddl_distribution_catalog_fallbacks() -> None:
    columns = pd.DataFrame([{"attnum": 1, "column_name": "id"}])
    calls = 0

    class UndefinedColumn(Exception):  # noqa: N818
        pass

    def fallback_read(_connection_key: str, _query: str) -> pd.DataFrame:
        nonlocal calls
        calls += 1
        if calls < 3:
            message = "legacy catalog"
            raise UndefinedColumn(message)
        return pd.DataFrame([{"policy_type": "replicated", "attrnums": None}])

    assert (
        gp_ddl.read_gp_distribution_clause(
            "gp",
            "1",
            columns,
            read_sql=fallback_read,
        )
        == "DISTRIBUTED REPLICATED"
    )

    def missing_shape(_connection_key: str, _query: str) -> pd.DataFrame:
        message = "legacy catalog"
        raise UndefinedColumn(message)

    assert (
        gp_ddl.read_gp_distribution_clause(
            "gp",
            "1",
            columns,
            read_sql=missing_shape,
        )
        == ""
    )

    with pytest.raises(RuntimeError, match="permission"):
        gp_ddl.read_gp_distribution_clause(
            "gp",
            "1",
            columns,
            read_sql=lambda *_args: (_ for _ in ()).throw(RuntimeError("permission")),
        )


def test_gp_ddl_helper_error_and_formatting_branches() -> None:
    row = pd.Series({"value": [1, 2]})
    assert gp_ddl.optional_metadata_value(row, "missing") is None
    assert gp_ddl.is_missing_value([1, 2]) is False
    with pytest.raises(ValueError, match="No metadata field"):
        gp_ddl.require_metadata_value(pd.Series(dtype=object), "oid", "events")

    assert (
        gp_ddl.format_gp_constraint_definition(pd.Series({"constraint_def": "CHECK (id > 0)"}))
        == "CHECK (id > 0)"
    )
    assert gp_ddl.format_gp_inherits_clause(pd.DataFrame()) == ""
    assert gp_ddl.format_gp_storage_clause(None) == ""
    assert gp_ddl.format_optional_statement(None) == ""
    assert (
        gp_ddl.format_gp_column_comment(
            '"public"."events"',
            pd.Series({"column_name": "id", "column_comment": None}),
        )
        == ""
    )
    assert gp_ddl.format_gp_distribution_clause(pd.DataFrame(), pd.DataFrame()) == ""
    assert (
        gp_ddl.format_gp_distribution_clause(
            pd.DataFrame([{"policy_type": "p", "attrnums": "{}"}]),
            pd.DataFrame(),
        )
        == "DISTRIBUTED RANDOMLY"
    )
    assert (
        gp_ddl.format_gp_distribution_clause(
            pd.DataFrame([{"policy_type": "p", "attrnums": "{9}"}]),
            pd.DataFrame([{"attnum": 1, "column_name": "id"}]),
        )
        == "DISTRIBUTED RANDOMLY"
    )

    assert gp_ddl.first_optional_value(pd.DataFrame(), "value") is None
    assert gp_ddl.first_optional_value(pd.DataFrame([{"other": 1}]), "value") is None
    assert gp_ddl.first_optional_value(pd.DataFrame([{"value": pd.NA}]), "value") is None
    assert gp_ddl.metadata_bool(pd.Series(dtype=object), "missing") is False
    assert gp_ddl.metadata_bool(pd.Series({"flag": pd.NA}), "flag") is False
    assert gp_ddl.metadata_bool(pd.Series({"flag": "yes"}), "flag") is True
    assert gp_ddl.parse_pg_array_text("['a', \"b\"]") == ["a", "b"]
    assert gp_ddl.parse_pg_array_text("plain") == ["plain"]
    assert gp_ddl.parse_attrnums("{-2,0,3}") == [3]

    class UndefinedFunction(Exception):  # noqa: N818
        pass

    assert gp_ddl.is_missing_pg_get_tabledef_error(UndefinedFunction("pg_get_tabledef unavailable"))
    assert not gp_ddl.is_missing_pg_get_tabledef_error(UndefinedFunction("different function"))
    assert gp_ddl.exception_text(Exception()) == ""


def test_gp_operation_backend_only_options_and_partition_requirements() -> None:
    with pytest.raises(InvalidSqlInputError, match="only supported for Trino"):
        gp_operations.build_show_tables_query(
            object(),
            object(),
            None,
            None,
            None,
            trino_catalog="hive",
        )
    with pytest.raises(InvalidSqlInputError, match="both start and end"):
        gp_operations.build_create_partition_sql(
            object(),
            "public.events",
            name="p1",
            start="2026-01-01",
        )


def test_gp_null_scalar_is_normalized_for_insert() -> None:
    assert gp_insert._is_null_like(None) is True
    assert gp_insert.normalize_insert_rows(object(), [[None]]) == [(None,)]


def test_clickhouse_metadata_application_and_macro_fallbacks() -> None:
    tables = pd.DataFrame({"name": ["events"]})
    assert (
        ch_metadata.apply_clickhouse_shard_stats(
            "ch",
            tables,
            read_sql=lambda *_args: pd.DataFrame(),
        )
        is tables
    )

    distributed = pd.DataFrame(
        [
            {
                "schema": "analytics",
                "engine": "Distributed",
                "engine_full": "Distributed('{cluster}', currentDatabase(), events_shard)",
            },
            {
                "schema": "analytics",
                "engine": "MergeTree",
                "engine_full": "MergeTree()",
            },
        ]
    )
    unchanged = ch_metadata.apply_clickhouse_shard_stats(
        "ch",
        distributed,
        read_sql=lambda *_args: pd.DataFrame(),
    )
    assert unchanged is distributed

    assert (
        ch_metadata.resolve_clickhouse_cluster_macro(
            "ch",
            "{cluster}",
            read_sql=lambda *_args: (_ for _ in ()).throw(RuntimeError("unavailable")),
        )
        == "{cluster}"
    )
    assert (
        ch_metadata.resolve_clickhouse_cluster_macro(
            "ch",
            "{cluster}",
            read_sql=lambda *_args: pd.DataFrame({"other": ["core"]}),
        )
        == "{cluster}"
    )
    assert (
        ch_metadata.resolve_clickhouse_cluster_macro(
            "ch",
            "{cluster}",
            read_sql=lambda *_args: pd.DataFrame({"cluster_name": [pd.NA]}),
        )
        == "{cluster}"
    )
    assert (
        ch_metadata.resolve_clickhouse_cluster_macro(
            "ch",
            "{cluster}",
            read_sql=lambda *_args: pd.DataFrame({"cluster_name": ["  "]}),
        )
        == "{cluster}"
    )


def test_clickhouse_shard_stats_handles_bad_rows_and_cluster_failures() -> None:
    good = ch_metadata.ClickHouseShardTable("core", "analytics", "events_shard")
    bad = ch_metadata.ClickHouseShardTable("broken", "analytics", "bad_shard")

    def read_sql(_connection_key: str, query: str) -> pd.DataFrame:
        if "'broken'" in query:
            message = "cluster unavailable"
            raise RuntimeError(message)
        return pd.DataFrame(
            [
                {"shard_database": 1, "shard_table": None},
                {
                    "shard_database": "analytics",
                    "shard_table": "events_shard",
                    "row_count": 7,
                    "table_size_bytes": 9,
                },
            ]
        )

    stats = ch_metadata.read_clickhouse_shard_stats(
        "ch",
        {good, bad},
        read_sql=read_sql,
    )
    assert stats[good] == (7, 9)
    assert bad not in stats


@pytest.mark.parametrize(
    ("engine_full", "database", "expected"),
    [
        (None, "analytics", None),
        ("MergeTree()", "analytics", None),
        ("Distributed('core', 'analytics')", "analytics", None),
        ("Distributed('', 'analytics', 'events')", "analytics", None),
        ("Distributed('core', currentDatabase(), 'events')", None, None),
        ("Distributed('core', currentDatabase(), 'events')", "", None),
    ],
)
def test_clickhouse_distributed_engine_invalid_forms(
    engine_full: object,
    database: object,
    expected: None,
) -> None:
    assert ch_metadata.extract_clickhouse_distributed_shard_table(engine_full, database) is expected


def test_clickhouse_engine_argument_and_parser_edges() -> None:
    assert ch_metadata.normalize_clickhouse_distributed_database_arg("", "db") is None
    assert ch_metadata.normalize_clickhouse_engine_arg("   ") is None
    assert ch_metadata.normalize_clickhouse_engine_arg("'it''s'") == "it's"
    assert ch_metadata.normalize_clickhouse_engine_arg('"a""b"') == 'a"b'
    assert ch_metadata.extract_clickhouse_function_args("Distributed(", "Distributed") is None
    assert (
        ch_metadata.find_clickhouse_function_call(
            "'Distributed(ignored)' Distributed ('core', 'db', 'table')",
            "Distributed",
        )
        == 23
    )
    assert (
        ch_metadata.find_clickhouse_function_call(
            "`Distributed` and fooDistributed('x')",
            "Distributed",
        )
        is None
    )
    assert ch_metadata.find_matching_paren("not a call", 0) is None
    assert ch_metadata.find_matching_paren("('a\\'b'", 0) is None
    assert ch_metadata.split_top_level_args("'a,b', nested(1, 2), , `x``y`") == [
        "'a,b'",
        "nested(1, 2)",
        "`x``y`",
    ]
    assert ch_metadata.skip_whitespace("  x", 0) == 2
    assert ch_metadata.is_clickhouse_identifier_boundary("a", -1)
    assert not ch_metadata.is_clickhouse_identifier_boundary("a", 0)


def test_clickhouse_wait_local_timeout_and_absence_paths() -> None:
    absent = RoutingClickHouseConnection(lambda _sql: [])
    with pytest.raises(TimeoutError, match="not visible"):
        ch_wait._wait_for_ch_table(absent, "analytics.events", timeout_seconds=0)

    present = RoutingClickHouseConnection(lambda _sql: [(1,)])
    with pytest.raises(TimeoutError, match="still visible"):
        ch_wait._wait_for_ch_table_absence(
            present,
            "analytics.events",
            timeout_seconds=0,
        )

    ch_wait._wait_for_ch_distributed_table_pair_absence(
        absent,
        "analytics.events",
        ch_cluster=None,
        timeout_seconds=0,
    )
    assert absent.queries[-2:] == [
        "EXISTS TABLE analytics.events",
        "EXISTS TABLE analytics.events_shard",
    ]


def test_clickhouse_wait_cluster_absence_empty_and_timeout_details() -> None:
    connection = RoutingClickHouseConnection(
        lambda sql: (
            [(1,)] if "count()" in sql else [("host-a", "analytics", "events", "Distributed")]
        )
    )
    ch_wait._wait_for_ch_tables_absence_on_cluster(
        connection,
        ["", "   "],
        ch_cluster="core",
    )
    with pytest.raises(TimeoutError, match="Leftover table"):
        ch_wait._wait_for_ch_tables_absence_on_cluster(
            connection,
            ["analytics.events"],
            ch_cluster="core",
            timeout_seconds=0,
        )

    failing = RoutingClickHouseConnection(lambda _sql: RuntimeError("metadata failed"))
    with pytest.raises(TimeoutError, match="still visible") as error:
        ch_wait._wait_for_ch_tables_absence_on_cluster(
            failing,
            ["analytics.events"],
            ch_cluster="core",
            timeout_seconds=0,
        )
    assert isinstance(error.value.__cause__, RuntimeError)


def test_clickhouse_wait_table_and_schema_timeout_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    empty = RoutingClickHouseConnection(lambda _sql: [])
    with pytest.raises(TimeoutError, match="not visible on every host"):
        ch_wait._wait_for_ch_table_on_cluster(
            empty,
            "analytics.events",
            ch_cluster="core",
            timeout_seconds=0,
        )

    failing = RoutingClickHouseConnection(lambda _sql: RuntimeError("metadata failed"))
    with pytest.raises(TimeoutError) as table_error:
        ch_wait._wait_for_ch_table_on_cluster(
            failing,
            "analytics.events",
            ch_cluster="core",
            timeout_seconds=0,
        )
    assert isinstance(table_error.value.__cause__, RuntimeError)

    with monkeypatch.context() as patch:
        patch.setattr(ch_wait, "normalize_table_schema", lambda *_args, **_kwargs: {})
        ch_wait._wait_for_ch_table_schema_on_cluster(
            empty,
            "analytics.events",
            expected_column_types={},
            ch_cluster="core",
        )

    def schema_route(sql: str) -> list[tuple[Any, ...]]:
        if "SELECT name, type, count()" in sql:
            return [("id", "String", 1), ("extra", "UInt8", 1), ("short",)]
        if "system.clusters" in sql or "system, one" in sql:
            return [(1,)]
        return [(0,)]

    schema_connection = RoutingClickHouseConnection(schema_route)
    with pytest.raises(TimeoutError, match="Schema mismatch details"):
        ch_wait._wait_for_ch_table_schema_on_cluster(
            schema_connection,
            "analytics.events",
            expected_column_types={"id": "UInt64", "missing": "String"},
            ch_cluster="core",
            timeout_seconds=0,
        )


def test_clickhouse_wait_schema_diagnostics_and_cluster_resolution() -> None:
    failing = RoutingClickHouseConnection(lambda _sql: RuntimeError("unavailable"))
    assert (
        ch_wait._describe_ch_cluster_schema_mismatch(
            failing,
            "analytics.events",
            expected_column_types={"id": "UInt64"},
            ch_cluster="core",
            expected_hosts=1,
        )
        == ""
    )

    matching = RoutingClickHouseConnection(lambda _sql: [("id", "UInt64", 1)])
    assert (
        ch_wait._describe_ch_cluster_schema_mismatch(
            matching,
            "analytics.events",
            expected_column_types={"id": "UInt64"},
            ch_cluster="core",
            expected_hosts=1,
        )
        == ""
    )

    assert ch_wait._resolve_ch_cluster_name_for_wait(matching, "'core'") == "core"
    macro_failure = RoutingClickHouseConnection(lambda _sql: RuntimeError("macro failed"))
    with pytest.raises(ValueError, match="Could not resolve"):
        ch_wait._resolve_ch_cluster_name_for_wait(macro_failure, "{cluster}")
    blank_macro = RoutingClickHouseConnection(lambda _sql: [("",)])
    with pytest.raises(ValueError, match="Could not resolve"):
        ch_wait._resolve_ch_cluster_name_for_wait(blank_macro, "{cluster}")

    assert ch_wait._strip_sql_wrapping_quotes("x") == "x"
    assert ch_wait._strip_sql_wrapping_quotes("'it''s'") == "it's"
    assert ch_wait._strip_sql_wrapping_quotes('"core"') == "core"
    assert ch_wait._extract_ch_macro_name("bad macro") is None


def test_clickhouse_wait_query_and_format_helpers() -> None:
    empty = RoutingClickHouseConnection(lambda _sql: [])
    assert ch_wait._query_ch_count(empty, "SELECT count()") == 0
    assert (
        ch_wait._query_ch_cluster_table_rows(
            empty,
            table_names=[],
            ch_cluster="core",
        )
        == []
    )

    rows = [("short",), *[(f"host-{index}", "db", "table", "MergeTree") for index in range(11)]]
    formatted = ch_wait._format_ch_cluster_table_rows(rows)
    assert formatted.endswith("...")
    assert "host-0: db.table" in formatted
    assert ch_wait._format_ch_cluster_table_rows([("short",)]) == ""

    calls = 0

    def host_route(_sql: str) -> list[tuple[int]] | RuntimeError:
        nonlocal calls
        calls += 1
        if calls == 2:
            return RuntimeError("system.clusters unavailable")
        return [(2,)]

    host_connection = RoutingClickHouseConnection(host_route)
    assert ch_wait._query_ch_cluster_host_counts(
        host_connection,
        cluster_name="core",
        remote_hosts_sql="remote",
    ) == (2, 2)


def test_clickhouse_operations_dispatches_missing_modes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lifecycle = importlib.import_module("analytics_toolkit.sql.backends.ch.lifecycle")
    calls: list[tuple[str, str, dict[str, Any]]] = []
    monkeypatch.setattr(
        lifecycle,
        "drop_ch_table",
        lambda _connection, table, **kwargs: calls.append(("table", table, kwargs)),
    )
    monkeypatch.setattr(
        lifecycle,
        "drop_ch_distributed_table_pair",
        lambda _connection, table, **kwargs: calls.append(("pair", table, kwargs)),
    )
    monkeypatch.setattr(
        importlib.import_module("analytics_toolkit.general"),
        "time_print",
        lambda *_args, **_kwargs: None,
    )

    with pytest.raises(ValueError, match="ch_drop_shard must be True"):
        ch_operations.drop_table_with_options(
            object(),
            object(),
            "analytics.events_shard",
            connection_key="ch",
            ch_drop_shard=False,
        )
    ch_operations.drop_table_with_options(
        object(),
        object(),
        "analytics.events",
        connection_key="ch",
        ch_drop_shard=False,
        ch_drop_distributed=True,
    )
    ch_operations.drop_table_with_options(
        object(),
        object(),
        "analytics.events",
        connection_key="ch",
        ch_drop_shard=True,
        ch_drop_distributed=False,
    )
    assert [call[:2] for call in calls] == [
        ("table", "analytics.events"),
        ("table", "analytics.events_shard"),
    ]
    with pytest.raises(ValueError, match="At least one"):
        ch_operations.drop_table_with_options(
            object(),
            object(),
            "analytics.events",
            connection_key="ch",
            ch_drop_shard=False,
            ch_drop_distributed=False,
        )


def test_clickhouse_operations_prepare_existing_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lifecycle = importlib.import_module("analytics_toolkit.sql.backends.ch.lifecycle")
    pair_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        lifecycle,
        "drop_ch_distributed_table_pair",
        lambda *_args, **kwargs: pair_calls.append(kwargs),
    )
    monkeypatch.setattr(
        importlib.import_module("analytics_toolkit.general"),
        "time_print",
        lambda *_args, **_kwargs: None,
    )

    adapter = SimpleNamespace(
        table_exists=lambda *_args, **_kwargs: True,
        drop_table=lambda *_args, **_kwargs: None,
    )
    assert (
        ch_operations.prepare_existing_target_for_create_from_sql(
            adapter,
            object(),
            "analytics.events",
            drop_target_if_exists=True,
            ch_only_shard=True,
        )
        is False
    )
    assert (
        ch_operations.prepare_existing_target_for_create_from_sql(
            adapter,
            object(),
            "analytics.events",
            drop_target_if_exists=True,
            connection_key=None,
        )
        is True
    )
    assert pair_calls[0]["per_host_connection_factory"] is None


def test_clickhouse_operations_small_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(InvalidSqlInputError, match="only supported for Trino"):
        ch_operations.build_show_tables_query(
            object(),
            object(),
            None,
            None,
            None,
            trino_catalog="hive",
        )

    connection = RoutingClickHouseConnection(lambda _sql: [("one",), (2,)])
    assert ch_operations.query_transfer_stage_table_names(
        object(),
        connection,
        connection_key="ch",
        transfer_staging_schema="staging",
        table_pattern="tmp_%",
    ) == ["one", "2"]
    assert (
        ch_operations.qualify_transfer_stage_table_name(
            object(),
            "ch",
            "staging",
            "tmp_1",
        )
        == "staging.tmp_1"
    )

    with pytest.raises(ValueError, match="ch_drop_shard must be True"):
        ch_operations.build_drop_tables_sqls(
            object(),
            "analytics.events_shard",
            ch_drop_shard=False,
        )

    with pytest.raises(ValueError, match="No DDL"):
        ch_operations._first_result_value(pd.DataFrame(), "events")
    with pytest.raises(ValueError, match="No DDL"):
        ch_operations._first_result_value(pd.DataFrame({"ddl": [pd.NA]}), "events")

    monkeypatch.setattr(ch_operations, "parse_one", lambda *_args, **_kwargs: exp.Column())
    assert ch_operations._is_default_ch_shard_table_name("events_shard") is False


def test_trino_operation_modes_and_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    config = SimpleNamespace(
        insert_chunk_size=5,
        s3_transfer_staging_location="s3://bucket/stage",
        upsert_partition_drop_sql_template="DELETE {partition}",
        catalog=None,
        connection_key="trino",
    )
    defaults = trino_operations.target_connection_defaults(object(), config)
    assert defaults.insert_chunk_size == 5
    assert defaults.s3_transfer_staging_location == "s3://bucket/stage"

    assert (
        trino_operations.resolve_transfer_staging_mode(
            object(),
            None,
            s3_transfer_staging_schema="stage",
            s3_transfer_staging_location="s3://bucket/stage",
        )
        == "parquet"
    )
    with pytest.raises(ValueError, match="requires s3_transfer_staging_schema"):
        trino_operations.resolve_transfer_staging_mode(
            object(),
            "parquet",
            s3_transfer_staging_schema=None,
            s3_transfer_staging_location="s3://bucket/stage",
        )
    with pytest.raises(ValueError, match="requires s3_transfer_staging_location"):
        trino_operations.resolve_transfer_staging_mode(
            object(),
            "parquet",
            s3_transfer_staging_schema="stage",
            s3_transfer_staging_location=None,
        )
    with pytest.raises(ValueError, match=r"requires.*catalog"):
        trino_operations.build_show_tables_query(object(), config, None, None, None)

    basic_ops = importlib.import_module("analytics_toolkit.sql.dml.table._basic_ops")
    monkeypatch.setattr(
        basic_ops,
        "get_trino_table_column_types",
        lambda *_args, **_kwargs: {"id": "bigint"},
    )
    assert trino_operations.resolve_transfer_stage_column_types(
        object(),
        object(),
        "stage.events",
        connection_key="trino",
        current_column_types=None,
    ) == {"id": "bigint"}


def test_trino_operation_cursor_and_error_paths() -> None:
    with pytest.raises(InvalidSqlInputError, match="Greenplum"):
        trino_operations.validate_drop_partitions_options(
            object(),
            partition_column="event_date",
            gp_truncate=True,
        )
    with pytest.raises(InvalidSqlInputError, match="partition_column"):
        trino_operations.validate_drop_partitions_options(
            object(),
            partition_column=None,
            gp_truncate=False,
        )
    with pytest.raises(InvalidSqlInputError, match="partition_column"):
        trino_operations.build_drop_partitions_sqls(
            object(),
            "hive.analytics.events",
            ["2025-01-01"],
        )

    cursor = RecordingCursor(rows=[("tmp_a",), (2,)])
    connection = RecordingConnection(cursor)
    assert trino_operations.query_transfer_stage_table_names(
        object(),
        connection,
        connection_key="trino",
        transfer_staging_schema="hive.stage",
        table_pattern="tmp_%",
    ) == ["tmp_a", "2"]
    assert cursor.closed is True

    with pytest.raises(ValueError, match="No DDL"):
        trino_operations._first_result_value(pd.DataFrame(), "events")
    with pytest.raises(ValueError, match="No DDL"):
        trino_operations._first_result_value(pd.DataFrame({"ddl": [pd.NA]}), "events")


def test_trino_stage_type_reuse_and_parquet_null_edge_cases() -> None:
    current = {"id": "BIGINT"}
    assert (
        trino_operations.resolve_transfer_stage_column_types(
            object(),
            object(),
            "stage",
            connection_key="warehouse",
            current_column_types=current,
        )
        is current
    )
    adapter = SimpleNamespace(sqlglot_dialect="trino")
    with pytest.raises(ValueError, match="Invalid target table name"):
        trino_parquet.parquet_stage_target_table_base(adapter, "function_call()")
    assert trino_parquet._infer_trino_type_from_values([None, pd.NA, 3]) == "BIGINT"
    assert trino_parquet._infer_trino_type_from_values([[1, 2]]) == "VARCHAR"
