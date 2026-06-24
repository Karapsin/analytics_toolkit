from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

schema_module = importlib.import_module("analytics_toolkit.sql.dml.transfer.schema")
stage_module = importlib.import_module("analytics_toolkit.sql.dml.load.stage")
transfer_stage_module = importlib.import_module(
    "analytics_toolkit.sql.dml.transfer.flow.stage"
)
transfer_finalize_module = importlib.import_module(
    "analytics_toolkit.sql.dml.transfer.flow.finalize"
)
runtime_models = importlib.import_module(
    "analytics_toolkit.sql.dml.transfer.runtime.models"
)


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
        "SELECT * FROM (select id, amount from source) "
        "AS source_schema_probe WHERE 1 = 0"
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
    assert len(stage_identifier.encode()) <= stage_module.GP_IDENTIFIER_MAX_BYTES
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
    assert len(stage_identifier.encode()) <= stage_module.GP_IDENTIFIER_MAX_BYTES
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
