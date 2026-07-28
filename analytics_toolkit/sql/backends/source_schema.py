from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from typing import Any

import pandas as pd

from .models import SourceColumn


def inspect_dbapi_source_schema(
    connection: Any,
    query: str,
    *,
    type_code_name: Callable[[Any, int | None, int | None], str | None],
) -> list[SourceColumn]:
    cursor = connection.cursor()
    try:
        cursor.execute(zero_row_query(query))
        return [
            source_column_from_description(column, type_code_name=type_code_name)
            for column in cursor.description or []
        ]
    finally:
        cursor.close()


def inspect_clickhouse_source_schema(connection: Any, query: str) -> list[SourceColumn]:
    result = connection.query(f"DESCRIBE TABLE ({strip_query_semicolon(query)})")
    rows = getattr(result, "result_rows", None) or []
    return [
        SourceColumn(name=str(row[0]), native_type=str(row[1]) if len(row) > 1 else None)
        for row in rows
    ]


def source_column_from_description(
    column: Any,
    *,
    type_code_name: Callable[[Any, int | None, int | None], str | None],
) -> SourceColumn:
    name = description_value(column, "name", 0)
    type_code = description_value(column, "type_code", 1)
    precision = optional_int(description_value(column, "precision", 4))
    scale = optional_int(description_value(column, "scale", 5))
    native_type = type_code_name(type_code, precision, scale)
    return SourceColumn(
        name=str(name),
        native_type=native_type,
        precision=precision,
        scale=scale,
    )


def description_value(column: Any, attribute: str, index: int) -> Any:
    if hasattr(column, attribute):
        return getattr(column, attribute)
    try:
        return column[index]
    except (IndexError, TypeError):
        return None


def zero_row_query(query: str) -> str:
    return (
        f"SELECT * FROM ({strip_query_semicolon(query)}) "
        "AS source_schema_probe WHERE 1 = 0"
    )


def strip_query_semicolon(query: str) -> str:
    stripped = query.strip()
    if stripped.endswith(";"):
        return stripped[:-1].strip()
    return stripped


def normalize_type_name(source_type: str | None) -> str:
    if not source_type:
        return ""
    normalized = source_type.strip().lower()
    while True:
        unwrapped = unwrap_type(normalized, "nullable")
        unwrapped = unwrap_type(unwrapped, "lowcardinality")
        if unwrapped == normalized:
            return normalized
        normalized = unwrapped


def unwrap_type(value: str, wrapper: str) -> str:
    prefix = f"{wrapper}("
    if value.startswith(prefix) and value.endswith(")"):
        return value[len(prefix) : -1].strip()
    return value


def classify_source_type(source_type: str) -> str:
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
        return "uuid" if source_type == "uuid" else "string"
    return "string"


def type_precision_scale(
    column: SourceColumn,
    source_type: str,
) -> tuple[int | None, int | None]:
    match = re.search(r"\((\d+)\s*,\s*(\d+)\)", source_type)
    if match:
        return int(match.group(1)), int(match.group(2))
    return column.precision, column.scale


def decimal_type(
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


def nullable_clickhouse_type(base_type: str) -> str:
    if base_type.startswith("Nullable("):
        return base_type
    return f"Nullable({base_type})"


def unwrap_nullable_clickhouse_type(type_name: str) -> str:
    normalized = type_name.strip()
    if normalized.startswith("Nullable(") and normalized.endswith(")"):
        return normalized[len("Nullable(") : -1].strip()
    return type_name


def refine_clickhouse_column_types_nullability_from_rows(
    column_types: dict[str, str] | None,
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
) -> dict[str, str] | None:
    if column_types is None or not rows:
        return column_types

    has_null = {column_name: False for column_name in columns}
    for row in rows:
        for column_name, value in zip(columns, row):
            if is_null_value(value):
                has_null[column_name] = True

    refined = dict(column_types)
    for column_name, type_name in column_types.items():
        if column_name not in has_null:
            continue
        refined[column_name] = (
            nullable_clickhouse_type(type_name)
            if has_null[column_name]
            else unwrap_nullable_clickhouse_type(type_name)
        )
    return refined


def is_null_value(value: Any) -> bool:
    if value is None:
        return True
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
