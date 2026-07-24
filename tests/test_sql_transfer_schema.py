from __future__ import annotations

import datetime as dt
import importlib
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest
from analytics_toolkit.sql.backend_adapters import get_backend_adapter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

schema_module = importlib.import_module("analytics_toolkit.sql.dml.transfer.schema")
stage_module = importlib.import_module("analytics_toolkit.sql.dml.load.stage")
gp_adapter_module = importlib.import_module("analytics_toolkit.sql.backends.gp.adapter")
transfer_stage_module = importlib.import_module("analytics_toolkit.sql.dml.transfer.flow.stage")
transfer_finalize_module = importlib.import_module(
    "analytics_toolkit.sql.dml.transfer.flow.finalize"
)
runtime_models = importlib.import_module("analytics_toolkit.sql.dml.transfer.runtime.models")
retry_module = importlib.import_module("analytics_toolkit.sql.dml.transfer.runtime.retry")
table_basic_module = importlib.import_module("analytics_toolkit.sql.dml.table._basic_ops")
identifiers_module = importlib.import_module("analytics_toolkit.sql.core.identifiers")


class FakeResult:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.result_rows = rows


class FakeClickHouseClient:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def query(self, sql: str) -> FakeResult:
        self.queries.append(sql)
        if sql == "DESCRIBE TABLE (select id, amount from source)":
            return FakeResult(
                [
                    ("id", "UInt64"),
                    ("amount", "Nullable(Decimal(18, 4))"),
                ]
            )
        if sql == "DESCRIBE TABLE target":
            return FakeResult(
                [
                    ("id", "Nullable(Int64)"),
                    ("amount", "Nullable(Decimal(18, 4))"),
                ]
            )
        return FakeResult([])


class FakeDbapiCursor:
    def __init__(self) -> None:
        self.executed: list[str] = []
        self.closed = False
        self.description = [
            ("id", 23, None, None, None, None),
            ("amount", 1700, None, None, 12, 2),
        ]

    def execute(self, sql: str) -> None:
        self.executed.append(sql)

    def close(self) -> None:
        self.closed = True


class FakeDbapiConnection:
    def __init__(self) -> None:
        self.cursor_obj = FakeDbapiCursor()

    def cursor(self) -> FakeDbapiCursor:
        return self.cursor_obj


def test_inspect_dbapi_source_schema_is_adapter_owned() -> None:
    connection = FakeDbapiConnection()

    result = schema_module.inspect_source_query_schema(
        "gp",
        connection,
        "select id, amount from source;",
    )

    assert result == [
        schema_module.SourceColumn("id", "integer"),
        schema_module.SourceColumn("amount", "numeric(12,2)", precision=12, scale=2),
    ]
    assert connection.cursor_obj.executed == [
        "SELECT * FROM (select id, amount from source) AS source_schema_probe WHERE 1 = 0"
    ]
    assert connection.cursor_obj.closed is True


def test_inspect_clickhouse_source_schema_is_adapter_owned() -> None:
    client = FakeClickHouseClient()

    result = schema_module.inspect_source_query_schema(
        "ch",
        client,
        "select id, amount from source;",
    )

    assert result == [
        schema_module.SourceColumn("id", "UInt64"),
        schema_module.SourceColumn("amount", "Nullable(Decimal(18, 4))"),
    ]
    assert client.queries == ["DESCRIBE TABLE (select id, amount from source)"]


def test_clickhouse_datetime64_timezone_is_attached_to_naive_batch_values() -> None:
    naive = dt.datetime(2026, 1, 2, 3, 4, 5, 123456)  # noqa: DTZ001
    batch = runtime_models.RowBatch(
        columns=["event_ts", "label"],
        rows=[(naive, "unchanged")],
    )

    normalized = get_backend_adapter("ch").normalize_transfer_source_batch(
        batch,
        {"event_ts": "Nullable(DateTime64(6, 'UTC'))", "label": "String"},
    )

    assert normalized.rows[0][0].isoformat() == "2026-01-02T03:04:05.123456+00:00"
    assert normalized.rows[0][1] == "unchanged"
    assert batch.rows[0][0].tzinfo is None


def test_clickhouse_source_batch_timezone_normalization_handles_noop_paths() -> None:
    aware = dt.datetime(2026, 1, 2, tzinfo=dt.timezone.utc)
    batch = runtime_models.RowBatch(columns=["event_ts"], rows=[(aware,)])
    adapter = get_backend_adapter("ch")

    unchanged_type = adapter.normalize_transfer_source_batch(
        batch,
        {"event_ts": "String"},
    )
    unchanged_value = adapter.normalize_transfer_source_batch(
        batch,
        {"event_ts": "DateTime64(6, 'UTC')"},
    )

    assert unchanged_type is batch
    assert unchanged_value.rows == [(aware,)]


def test_clickhouse_upsert_finalization_error_is_marked_unsafe() -> None:
    error = RuntimeError("ambiguous finalization")

    get_backend_adapter("ch").mark_upsert_finalization_error(error)

    assert error.__dict__["analytics_toolkit_sql_retry_safe"] is False


def test_retry_marker_prevents_unsafe_operation_retry() -> None:
    attempts: list[int] = []

    def operation(attempt: int) -> None:
        attempts.append(attempt)
        error = RuntimeError("ambiguous finalization")
        error.analytics_toolkit_sql_retry_safe = False
        raise error

    with pytest.raises(RuntimeError, match="ambiguous finalization"):
        retry_module.run_with_retry("unsafe write", 3, 0, operation)

    assert attempts == [1]


def test_map_source_schema_to_target_preserves_common_types() -> None:
    source_schema = [
        schema_module.SourceColumn("is_active", "boolean"),
        schema_module.SourceColumn("user_id", "integer"),
        schema_module.SourceColumn("amount", "numeric(12, 2)"),
        schema_module.SourceColumn("created_at", "timestamp"),
        schema_module.SourceColumn("payload", "jsonb"),
    ]

    assert schema_module.map_source_schema_to_target(source_schema, "gp") == {
        "is_active": "BOOLEAN",
        "user_id": "INTEGER",
        "amount": "NUMERIC(12, 2)",
        "created_at": "TIMESTAMP",
        "payload": "TEXT",
    }
    assert schema_module.map_source_schema_to_target(source_schema, "trino") == {
        "is_active": "BOOLEAN",
        "user_id": "INTEGER",
        "amount": "DECIMAL(12, 2)",
        "created_at": "TIMESTAMP",
        "payload": "VARCHAR",
    }
    assert schema_module.map_source_schema_to_target(source_schema, "ch") == {
        "is_active": "Nullable(Bool)",
        "user_id": "Nullable(Int32)",
        "amount": "Nullable(Decimal(12, 2))",
        "created_at": "Nullable(DateTime64(6))",
        "payload": "Nullable(String)",
    }


def test_map_source_schema_to_target_preserves_binary_types() -> None:
    source_schema = [
        schema_module.SourceColumn("cheque_pk", "bytea"),
        schema_module.SourceColumn("raw_payload", "varbinary"),
    ]

    assert schema_module.map_source_schema_to_target(source_schema, "gp") == {
        "cheque_pk": "BYTEA",
        "raw_payload": "BYTEA",
    }
    assert schema_module.map_source_schema_to_target(source_schema, "trino") == {
        "cheque_pk": "VARBINARY",
        "raw_payload": "VARBINARY",
    }
    assert schema_module.map_source_schema_to_target(source_schema, "ch") == {
        "cheque_pk": "Nullable(String)",
        "raw_payload": "Nullable(String)",
    }


def test_map_source_schema_to_target_preserves_same_trino_native_types() -> None:
    source_schema = [
        schema_module.SourceColumn("campaign_codes", "array(varchar)"),
        schema_module.SourceColumn("po_bonus_pk", "array(varbinary)"),
        schema_module.SourceColumn("attributes", "map(varchar, bigint)"),
        schema_module.SourceColumn(
            "details",
            "row(campaign_code varchar, payload varbinary)",
        ),
        schema_module.SourceColumn("untyped_value", "unknown"),
    ]

    assert schema_module.map_source_schema_to_target(
        source_schema,
        "trino",
        source_backend="trino",
    ) == {
        "campaign_codes": "array(varchar)",
        "po_bonus_pk": "array(varbinary)",
        "attributes": "map(varchar, bigint)",
        "details": "row(campaign_code varchar, payload varbinary)",
        "untyped_value": "VARCHAR",
    }
    assert schema_module.map_source_schema_to_target(source_schema, "trino") == {
        "campaign_codes": "VARCHAR",
        "po_bonus_pk": "VARCHAR",
        "attributes": "BIGINT",
        "details": "VARCHAR",
        "untyped_value": "VARCHAR",
    }


def test_map_source_schema_to_target_falls_back_for_invalid_decimal_bounds() -> None:
    source_schema = [
        schema_module.SourceColumn("quantity", "numeric(65535, 0)"),
    ]

    assert schema_module.map_source_schema_to_target(source_schema, "gp") == {
        "quantity": "NUMERIC",
    }
    assert schema_module.map_source_schema_to_target(source_schema, "trino") == {
        "quantity": "DECIMAL(38, 10)",
    }
    assert schema_module.map_source_schema_to_target(source_schema, "ch") == {
        "quantity": "Nullable(Decimal(38, 10))",
    }


def test_build_stage_table_name_uses_transfer_staging_schema_and_username() -> None:
    stage_name = stage_module.build_stage_table_name(
        "gp",
        "sales.target",
        transfer_staging_schema="transfer_schema",
        transfer_staging_username="loader",
        random_suffix="abcd",
    )

    assert stage_name == "transfer_schema.target__analytics_toolkit_loader__stage__abcd"


def test_build_stage_table_name_keeps_gp_identifier_within_limit() -> None:
    stage_name = stage_module.build_stage_table_name(
        "gp",
        "sales.karapsin_temp_users_po",
        transfer_staging_schema="transfer_schema",
        transfer_staging_username="karapsin_de",
        random_suffix="4f99601c",
    )

    stage_identifier = stage_name.split(".")[-1]
    assert len(stage_identifier.encode()) <= gp_adapter_module.GP_IDENTIFIER_MAX_BYTES
    assert stage_identifier.endswith("__stage__4f99601c")
    assert stage_identifier.startswith("karap")
    assert not stage_identifier.startswith("karapsin_temp_users_po__")


def test_build_stage_table_name_keeps_gp_identifier_within_limit_without_username() -> None:
    stage_name = stage_module.build_stage_table_name(
        "gp",
        "sales.very_long_target_table_name_for_monthly_analytics_exports",
        transfer_staging_schema="transfer_schema",
        random_suffix="4f99601c",
    )

    stage_identifier = stage_name.split(".")[-1]
    assert len(stage_identifier.encode()) <= gp_adapter_module.GP_IDENTIFIER_MAX_BYTES
    assert stage_identifier.endswith("__stage__4f99601c")
    assert "__analytics_toolkit_" not in stage_identifier
    assert not stage_identifier.startswith(
        "very_long_target_table_name_for_monthly_analytics_exports__"
    )


def test_build_stage_table_name_keeps_legacy_naming_without_transfer_schema() -> None:
    stage_name = stage_module.build_stage_table_name(
        "gp",
        "sales.target",
        random_suffix="abcd",
    )

    assert stage_name == "sales.target__stage__abcd"


def test_create_stage_table_retries_collision_and_uses_explicit_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage_names = iter(["sales.target__stage__first", "sales.target__stage__second"])
    existence_checks: list[tuple[str, str]] = []
    created: dict[str, object] = {}
    messages: list[str] = []
    connection = object()

    monkeypatch.setattr(
        stage_module,
        "build_stage_table_name",
        lambda *args, **kwargs: next(stage_names),
    )

    def fake_table_exists(
        connection_type: str,
        connection: object,
        table_name: str,
        *,
        connection_key: str,
    ) -> bool:
        del connection
        existence_checks.append((table_name, connection_key))
        return table_name.endswith("first")

    def fake_create(*args: object, **kwargs: object) -> None:
        created["args"] = args
        created["kwargs"] = kwargs

    monkeypatch.setattr(stage_module, "table_exists", fake_table_exists)
    monkeypatch.setattr(stage_module, "_create_sql_table_with_connection", fake_create)
    monkeypatch.setattr(stage_module, "time_print", messages.append)

    result = stage_module.create_stage_table(
        "gp",
        connection,
        "sales.target",
        pd.DataFrame({"id": [1]}),
        column_types={"id": "INTEGER"},
        table_schema={"id": "BIGINT"},
        gp_distributed_by_key=["id"],
        connection_key="warehouse",
        query_label="load stage",
    )

    assert result == "sales.target__stage__second"
    assert existence_checks == [
        ("sales.target__stage__first", "warehouse"),
        ("sales.target__stage__second", "warehouse"),
    ]
    assert messages == [
        "Stage table name collision detected for sales.target__stage__first; "
        "retrying with a new name (1/10)"
    ]
    assert created["args"][0:4] == (
        "gp",
        connection,
        "sales.target__stage__second",
        None,
    )
    assert created["kwargs"] == {
            "connection_key": "warehouse",
            "ddl_scope": "staging",
            "gp_distributed_by_key": ["id"],
        "query_label": "load stage",
        "table_schema": {"id": "BIGINT"},
    }


def test_create_stage_table_fixed_suffix_collision_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        stage_module,
        "table_exists",
        lambda *args, **kwargs: True,
    )

    with pytest.raises(RuntimeError, match="Stage table name collision detected"):
        stage_module.create_stage_table(
            "gp",
            object(),
            "sales.target",
            pd.DataFrame({"id": [1]}),
            random_suffix="fixed",
        )


def test_create_stage_table_exhausts_generated_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(stage_module, "STAGE_TABLE_NAME_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(stage_module, "table_exists", lambda *args, **kwargs: True)
    monkeypatch.setattr(stage_module, "time_print", lambda message: None)

    with pytest.raises(RuntimeError, match="unique stage table name after 2 attempts"):
        stage_module.create_stage_table(
            "trino",
            object(),
            "sales.target",
            pd.DataFrame({"id": [1]}),
        )


def test_create_stage_table_uses_batch_when_schema_is_not_supplied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batch = pd.DataFrame({"id": [1]})
    captured: dict[str, object] = {}
    monkeypatch.setattr(stage_module, "table_exists", lambda *args, **kwargs: False)

    def fake_create(*args: object, **kwargs: object) -> None:
        captured["args"] = args
        captured["kwargs"] = kwargs

    monkeypatch.setattr(stage_module, "_create_sql_table_with_connection", fake_create)

    result = stage_module.create_stage_table(
        "ch",
        object(),
        "analytics.target",
        batch,
        random_suffix="fixed",
    )

    assert result == "analytics.target__stage__fixed"
    assert captured["args"][3] is batch
    assert captured["kwargs"] == {
        "connection_key": "ch",
        "ddl_scope": "staging",
        "gp_distributed_by_key": None,
    }


def test_stage_cleanup_helpers_forward_retry_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    direct_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    retry_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    monkeypatch.setattr(
        stage_module,
        "drop_table",
        lambda *args, **kwargs: direct_calls.append((args, kwargs)),
    )
    monkeypatch.setattr(
        stage_module,
        "drop_table_with_retry",
        lambda *args, **kwargs: retry_calls.append((args, kwargs)),
    )

    stage_module.cleanup_stage_table(
        "ch",
        "connection",
        "analytics.stage",
        query_label="cleanup",
        if_exists=False,
    )
    retry_fn = object()
    rollback_fn = object()
    replace_connection_fn = object()
    connection_ref = {"connection": "old"}
    stage_module.cleanup_stage_table_with_retry(
        "gp",
        "warehouse",
        connection_ref,
        "sales.stage",
        retry_fn=retry_fn,
        retry_cnt=3,
        timeout_increment=0.5,
        rollback_fn=rollback_fn,
        replace_connection_fn=replace_connection_fn,
        query_label="cleanup retry",
        if_exists=False,
    )

    assert direct_calls == [
        (
            ("ch", "connection", "analytics.stage"),
            {"query_label": "cleanup", "if_exists": False},
        )
    ]
    assert retry_calls == [
        (
            ("gp", "warehouse", connection_ref, "sales.stage"),
            {
                "retry_fn": retry_fn,
                "retry_cnt": 3,
                "timeout_increment": 0.5,
                "rollback_fn": rollback_fn,
                "replace_connection_fn": replace_connection_fn,
                "query_label": "cleanup retry",
                "if_exists": False,
            },
        )
    ]


@pytest.mark.parametrize(
    ("username", "expected"),
    [
        (None, "target__stage__"),
        ("loader", "target__analytics_toolkit_loader__stage__"),
    ],
)
def test_build_stage_table_prefix(username: str | None, expected: str) -> None:
    assert stage_module.build_stage_table_prefix("gp", "sales.target", username) == expected


def test_refine_clickhouse_nullability_from_rows() -> None:
    result = schema_module.refine_ch_column_types_nullability_from_rows(
        {
            "dt": "Nullable(Date)",
            "amount": "Nullable(Decimal(12, 2))",
            "label": "String",
        },
        ["dt", "amount", "label"],
        [
            ("2024-01-01", None, "a"),
            ("2024-01-02", "1.20", "b"),
        ],
    )

    assert result == {
        "dt": "Date",
        "amount": "Nullable(Decimal(12, 2))",
        "label": "String",
    }


def test_refine_stage_column_types_noops_for_non_clickhouse_backends() -> None:
    column_types = {"id": "INTEGER"}

    assert (
        schema_module.refine_stage_column_types_from_rows(
            "gp",
            column_types,
            ["id"],
            [(None,)],
        )
        is column_types
    )


def test_transfer_table_schema_overrides_clickhouse_nullability_refinement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_create_stage_table(**kwargs: object) -> str:
        captured.update(kwargs)
        return "target__stage"

    monkeypatch.setattr(
        transfer_stage_module,
        "create_stage_table",
        fake_create_stage_table,
    )

    options = runtime_models.TransferOptions(
        from_db_key="gp",
        from_db_backend="gp",
        to_db_key="ch",
        to_db_backend="ch",
        source_sql="select id from source_table",
        target_table="target",
        table_schema={"id": "String"},
    )
    stage_state = runtime_models.TransferStageState(
        target_exists=False,
        stage_column_types={"id": "Nullable(Int64)"},
    )

    transfer_stage_module.initialize_stage_for_first_batch(
        options=options,
        connection_refs=runtime_models.TransferConnectionRefs(
            target={"connection": object()},
        ),
        stage_state=stage_state,
        batch=runtime_models.RowBatch(columns=["id"], rows=[(1,)]),
    )

    assert stage_state.stage_column_types == {"id": "String"}
    assert captured["column_types"] == {"id": "String"}
    assert stage_state.stage_table == "target__stage"


def test_existing_target_insert_types_use_target_metadata() -> None:
    client = FakeClickHouseClient()

    result = schema_module.get_existing_target_insert_types(
        "ch",
        client,
        "target",
        {
            "id": "Nullable(Int32)",
            "amount": "Nullable(Float64)",
        },
        connection_key="ch",
    )

    assert result == {
        "id": "Nullable(Int64)",
        "amount": "Nullable(Decimal(18, 4))",
    }


def test_existing_target_insert_types_reject_missing_columns() -> None:
    client = FakeClickHouseClient()

    try:
        schema_module.get_existing_target_insert_types(
            "ch",
            client,
            "target",
            {
                "id": "Nullable(Int32)",
                "missing": "Nullable(String)",
            },
            connection_key="ch",
        )
    except ValueError as exc:
        assert "missing" in str(exc)
    else:
        raise AssertionError("Expected missing target column to raise.")


def test_transfer_upsert_duplicate_stage_keys_raise_before_finalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = runtime_models.TransferOptions(
        from_db_key="gp",
        from_db_backend="gp",
        to_db_key="gp_sandbox",
        to_db_backend="gp",
        source_sql="select id, value from source_table",
        target_table="sandbox.target",
        write_mode="upsert",
        key_columns=["id"],
    )
    stage_state = runtime_models.TransferStageState(
        target_exists=True,
        stage_table="sandbox.target__stage",
        stage_table_created=True,
        first_non_empty_batch=runtime_models.RowBatch(
            columns=["id", "value"],
            rows=[(1, 10)],
        ).to_dataframe(include_rows=True),
        stage_column_types={"id": "BIGINT", "value": "INTEGER"},
    )
    finalized = False

    def fake_validate_stage_uniqueness(*args: object, **kwargs: object) -> None:
        raise ValueError("Duplicate key values found in staged data")

    def fake_finalize_stage_table(*args: object, **kwargs: object) -> None:
        nonlocal finalized
        finalized = True

    monkeypatch.setattr(
        transfer_finalize_module,
        "validate_stage_uniqueness",
        fake_validate_stage_uniqueness,
    )
    monkeypatch.setattr(
        transfer_finalize_module,
        "finalize_stage_table",
        fake_finalize_stage_table,
    )
    monkeypatch.setattr(
        transfer_finalize_module,
        "get_sql_connection",
        lambda key: object(),
    )

    with pytest.raises(ValueError, match="Duplicate key"):
        transfer_finalize_module.finalize_loaded_stage(
            options=options,
            connection_refs=runtime_models.TransferConnectionRefs(
                target={"connection": object()},
            ),
            stage_state=stage_state,
            total_rows=1,
        )

    assert finalized is False


def test_stage_name_parser_guards_reject_injected_invalid_nodes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(stage_module, "parse_one", lambda *_args, **_kwargs: object())
    with pytest.raises(ValueError, match="Invalid target table"):
        stage_module.build_stage_table_name("gp", "sandbox.target")
    with pytest.raises(ValueError, match="Invalid target table"):
        stage_module.build_stage_table_prefix("gp", "sandbox.target", None)

    valid_target = stage_module.exp.Table(this=stage_module.exp.Identifier(this="target"))
    parsed = iter([valid_target, object()])
    monkeypatch.setattr(stage_module, "parse_one", lambda *_args, **_kwargs: next(parsed))
    with pytest.raises(ValueError, match="Invalid transfer_staging_schema"):
        stage_module.build_stage_table_name(
            "gp",
            "sandbox.target",
            transfer_staging_schema="scratch",
        )


def test_table_basic_compatibility_delegates_and_identifier_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert (
        table_basic_module._format_gp_information_schema_type(
            "numeric",
            "numeric",
            10,
            2,
        )
        == "NUMERIC(10, 2)"
    )
    assert table_basic_module._extract_row_count({"rows": 4}) == 4
    monkeypatch.setattr(
        identifiers_module.TableIdentifier,
        "parse",
        classmethod(lambda _cls, *_args: SimpleNamespace(parts=("one", "two", "three", "four"))),
    )
    with pytest.raises(Exception, match="up to three parts"):
        table_basic_module.quote_qualified_table_name("one.two.three.four", "trino")
