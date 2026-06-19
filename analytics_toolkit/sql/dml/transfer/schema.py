from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import pandas as pd

from ...backend_adapters import get_backend_adapter
from ...backends.gp.adapter import _GP_OID_TYPES
from ..table._basic_ops import get_table_column_types


@dataclass(frozen=True)
class SourceColumn:
    name: str
    native_type: str | None = None
    precision: int | None = None
    scale: int | None = None


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


def refine_ch_column_types_nullability_from_rows(
    column_types: dict[str, str] | None,
    columns: list[str],
    rows: list[tuple[Any, ...]],
) -> dict[str, str] | None:
    if column_types is None or not rows:
        return column_types

    has_null = {column_name: False for column_name in columns}
    for row in rows:
        for column_name, value in zip(columns, row):
            if _is_null_value(value):
                has_null[column_name] = True

    refined = dict(column_types)
    for column_name, type_name in column_types.items():
        if column_name not in has_null:
            continue
        refined[column_name] = (
            _nullable_ch_type(type_name)
            if has_null[column_name]
            else _unwrap_nullable_ch_type(type_name)
        )
    return refined


def map_source_type_to_target(column: SourceColumn, target_backend: str) -> str:
    return get_backend_adapter(target_backend).map_source_type_to_target(column)


def _inspect_dbapi_source_schema(
    connection_backend: str,
    connection: Any,
    query: str,
) -> list[SourceColumn]:
    cursor = connection.cursor()
    try:
        cursor.execute(_zero_row_query(query))
        return [
            _source_column_from_description(connection_backend, column)
            for column in cursor.description or []
        ]
    finally:
        cursor.close()


def _inspect_ch_source_schema(connection: Any, query: str) -> list[SourceColumn]:
    result = connection.query(f"DESCRIBE TABLE ({_strip_query_semicolon(query)})")
    rows = getattr(result, "result_rows", None) or []
    return [
        SourceColumn(name=str(row[0]), native_type=str(row[1]) if len(row) > 1 else None)
        for row in rows
    ]


def _source_column_from_description(
    connection_backend: str,
    column: Any,
) -> SourceColumn:
    name = _description_value(column, "name", 0)
    type_code = _description_value(column, "type_code", 1)
    precision = _optional_int(_description_value(column, "precision", 4))
    scale = _optional_int(_description_value(column, "scale", 5))
    native_type = _type_code_name(connection_backend, type_code, precision, scale)
    return SourceColumn(
        name=str(name),
        native_type=native_type,
        precision=precision,
        scale=scale,
    )


def _description_value(column: Any, attribute: str, index: int) -> Any:
    if hasattr(column, attribute):
        return getattr(column, attribute)
    try:
        return column[index]
    except (IndexError, TypeError):
        return None


def _type_code_name(
    connection_backend: str,
    type_code: Any,
    precision: int | None,
    scale: int | None,
) -> str | None:
    return get_backend_adapter(connection_backend).type_code_name(
        type_code,
        precision,
        scale,
    )


def _zero_row_query(query: str) -> str:
    return f"SELECT * FROM ({_strip_query_semicolon(query)}) AS source_schema_probe WHERE 1 = 0"


def _strip_query_semicolon(query: str) -> str:
    stripped = query.strip()
    if stripped.endswith(";"):
        return stripped[:-1].strip()
    return stripped


def _normalize_type_name(source_type: str | None) -> str:
    if not source_type:
        return ""
    normalized = source_type.strip().lower()
    while True:
        unwrapped = _unwrap_type(normalized, "nullable")
        unwrapped = _unwrap_type(unwrapped, "lowcardinality")
        if unwrapped == normalized:
            return normalized
        normalized = unwrapped


def _unwrap_type(value: str, wrapper: str) -> str:
    prefix = f"{wrapper}("
    if value.startswith(prefix) and value.endswith(")"):
        return value[len(prefix) : -1].strip()
    return value


def _classify_source_type(source_type: str) -> str:
    if not source_type:
        return "string"
    if source_type in {"binary", "bytea", "varbinary"}:
        return "binary"
    if source_type in {"boolean", "bool"}:
        return "boolean"
    if source_type in {"date", "date32"}:
        return "date"
    if "timestamp" in source_type or source_type.startswith("datetime"):
        return "timestamp"
    if source_type.startswith(("decimal", "numeric", "number")):
        return "decimal"
    if source_type.startswith(("float", "double", "real")):
        return "float"
    if "int" in source_type and not source_type.startswith("interval"):
        return "integer"
    if any(
        token in source_type
        for token in (
            "char",
            "text",
            "string",
            "uuid",
            "json",
            "enum",
            "ip",
        )
    ):
        return "string"
    return "string"


def _type_precision_scale(
    column: SourceColumn,
    source_type: str,
) -> tuple[int | None, int | None]:
    match = re.search(r"\((\d+)\s*,\s*(\d+)\)", source_type)
    if match:
        return int(match.group(1)), int(match.group(2))
    return column.precision, column.scale


def _map_to_gp_type(
    kind: str,
    source_type: str,
    precision: int | None,
    scale: int | None,
) -> str:
    from ...backends.gp.adapter import _map_to_gp_type as map_to_gp_type

    return map_to_gp_type(kind, source_type, precision, scale)


def _map_to_trino_type(
    kind: str,
    source_type: str,
    precision: int | None,
    scale: int | None,
) -> str:
    from ...backends.trino.adapter import _map_to_trino_type as map_to_trino_type

    return map_to_trino_type(kind, source_type, precision, scale)


def _map_to_ch_base_type(
    kind: str,
    source_type: str,
    precision: int | None,
    scale: int | None,
) -> str:
    from ...backends.ch.adapter import _map_to_ch_base_type as map_to_ch_base_type

    return map_to_ch_base_type(kind, source_type, precision, scale)


def _decimal_type(
    name: str,
    precision: int | None,
    scale: int | None,
    fallback: str,
    max_precision: int,
) -> str:
    if (
        precision is None
        or scale is None
        or precision < 1
        or precision > max_precision
        or scale < 0
        or scale > precision
    ):
        return fallback
    return f"{name}({precision}, {scale})"


def _nullable_ch_type(base_type: str) -> str:
    if base_type.startswith("Nullable("):
        return base_type
    return f"Nullable({base_type})"


def _unwrap_nullable_ch_type(type_name: str) -> str:
    normalized = type_name.strip()
    if normalized.startswith("Nullable(") and normalized.endswith(")"):
        return normalized[len("Nullable(") : -1].strip()
    return type_name


def _is_null_value(value: Any) -> bool:
    if value is None:
        return True
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
