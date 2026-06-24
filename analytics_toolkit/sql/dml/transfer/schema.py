from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from ...backend_adapters import get_backend_adapter
from ...backends.models import SourceColumn
from ..table._basic_ops import get_table_column_types


def inspect_source_query_schema(
    connection_backend: str,
    connection: Any,
    query: str,
) -> list[SourceColumn]:
    return get_backend_adapter(connection_backend).inspect_source_query_schema(
        connection,
        query,
    )


def map_source_schema_to_target(
    source_schema: list[SourceColumn],
    target_backend: str,
) -> dict[str, str]:
    return {
        column.name: map_source_type_to_target(column, target_backend)
        for column in source_schema
    }


def map_source_type_to_target(column: SourceColumn, target_backend: str) -> str:
    return get_backend_adapter(target_backend).map_source_type_to_target(column)


def refine_stage_column_types_from_rows(
    target_backend: str,
    column_types: dict[str, str] | None,
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
) -> dict[str, str] | None:
    return get_backend_adapter(target_backend).refine_stage_column_types_from_rows(
        column_types,
        columns,
        rows,
    )


def refine_ch_column_types_nullability_from_rows(
    column_types: dict[str, str] | None,
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
) -> dict[str, str] | None:
    return refine_stage_column_types_from_rows("ch", column_types, columns, rows)


def get_existing_target_insert_types(
    connection_backend: str,
    connection: Any,
    target_table: str,
    stage_column_types: dict[str, str],
    connection_key: str,
) -> dict[str, str]:
    target_column_types = get_table_column_types(
        connection_backend,
        connection,
        target_table,
        connection_key=connection_key,
    )
    missing_columns = [
        column_name
        for column_name in stage_column_types
        if column_name not in target_column_types
    ]
    if missing_columns:
        raise ValueError(
            "Target table is missing staged column(s): "
            + ", ".join(missing_columns)
        )
    return {
        column_name: target_column_types[column_name]
        for column_name in stage_column_types
    }
