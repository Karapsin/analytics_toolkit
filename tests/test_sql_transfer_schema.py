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
        if sql == "DESCRIBE TABLE target":
            return FakeResult(
                [
                    ("id", "Nullable(Int64)"),
                    ("amount", "Nullable(Decimal(18, 4))"),
                ]
            )
        return FakeResult([])


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
