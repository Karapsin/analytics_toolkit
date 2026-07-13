from __future__ import annotations

import pandas as pd
import pytest
from analytics_toolkit.sql.ddl import schema


def test_build_table_schema_definitions_preserves_requested_order() -> None:
    definitions = schema.build_table_schema_column_definitions(
        "gp",
        {"name": " TEXT ", "id": "BIGINT"},
        columns=["id", "name"],
    )

    assert definitions == '"id" BIGINT, "name" TEXT'


@pytest.mark.parametrize(
    ("table_schema", "error_type", "message"),
    [
        ({}, ValueError, "must not be empty"),
        ({"": "TEXT"}, ValueError, "column names"),
        ({1: "TEXT"}, ValueError, "column names"),
        ({"id": 1}, TypeError, "must be a string"),
        ({"id": " "}, ValueError, "must not be empty"),
    ],
)
def test_normalize_table_schema_rejects_invalid_entries(
    table_schema: object,
    error_type: type[Exception],
    message: str,
) -> None:
    with pytest.raises(error_type, match=message):
        schema.normalize_table_schema(table_schema)


def test_validate_table_schema_columns_reports_missing_and_extra_columns() -> None:
    with pytest.raises(ValueError, match=r"missing SQL type.*name"):
        schema.validate_table_schema_columns({"id": "BIGINT"}, ["id", "name"])

    with pytest.raises(ValueError, match=r"not present in data.*name"):
        schema.validate_table_schema_columns(
            {"id": "BIGINT", "name": "TEXT"},
            ["id"],
        )

    assert schema.validate_table_schema_columns(
        {"name": "TEXT", "id": "BIGINT"},
        ["id", "name"],
    ) == {"id": "BIGINT", "name": "TEXT"}


def test_resolve_create_column_types_handles_each_source_combination() -> None:
    columns = ["id", "name"]
    explicit = {"id": "BIGINT", "name": "TEXT"}

    assert (
        schema._resolve_create_column_types(
            table_schema=None,
            column_types=explicit,
            columns=columns,
        )
        is explicit
    )
    assert (
        schema._resolve_create_column_types(
            table_schema=explicit,
            column_types=None,
            columns=columns,
        )
        == explicit
    )
    assert (
        schema._resolve_create_column_types(
            table_schema=explicit,
            column_types={"id": " BIGINT ", "name": " TEXT "},
            columns=columns,
        )
        == explicit
    )

    with pytest.raises(ValueError, match="same SQL types"):
        schema._resolve_create_column_types(
            table_schema=explicit,
            column_types={"id": "INTEGER", "name": "TEXT"},
            columns=columns,
        )


def test_explicit_column_types_validate_mapping_keys_and_values() -> None:
    with pytest.raises(TypeError, match="column_types must be a mapping"):
        schema._normalize_column_types_for_columns([], ["id"])
    with pytest.raises(ValueError, match="Missing explicit SQL type"):
        schema._normalize_column_types_for_columns({}, ["id"])
    with pytest.raises(ValueError, match="must not be empty"):
        schema._explicit_column_type({"id": " "}, "id")


def test_build_column_definitions_infers_types_without_overrides() -> None:
    definitions = schema._build_column_definitions(
        "gp",
        pd.DataFrame({"id": pd.Series([1], dtype="int64")}),
        None,
    )

    assert definitions == '"id" BIGINT'
