from __future__ import annotations

import re
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pandas as pd

from .models import SourceColumn

_TRINO_MAX_DECIMAL_PRECISION = 38

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence


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
    return f"SELECT * FROM ({strip_query_semicolon(query)}) AS source_schema_probe WHERE 1 = 0"


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


def normalize_source_column(column: SourceColumn, source_backend: str | None) -> SourceColumn:
    if source_backend != "gp" or not column.native_type:
        return column
    match = re.fullmatch(
        r"(int2|int4|int8)((?:\[\])*)", column.native_type.strip(), flags=re.IGNORECASE
    )
    if match is None:
        return column
    scalar = {"int2": "SMALLINT", "int4": "INTEGER", "int8": "BIGINT"}[match.group(1).lower()]
    return replace(column, native_type=scalar + match.group(2))


def map_source_column(
    column: SourceColumn, target_backend: str, source_backend: str | None = None
) -> str:
    from analytics_toolkit.sql.backends import (  # noqa: PLC0415 - backend recursive type policies
        get_backend_adapter,
    )

    from .nested_types import (  # noqa: PLC0415 - nested native policy
        map_declared_nested,
        needs_timestamp_adaptation,
    )

    column = normalize_source_column(column, source_backend)
    adapter = get_backend_adapter(target_backend)
    if source_backend == target_backend and (
        target_backend != "trino" or not needs_timestamp_adaptation(column.native_type or "")
    ):
        return str(adapter.map_same_backend_source_type_to_target(column))
    if source_backend and column.native_type:
        nested = map_declared_nested(column.native_type, target_backend, source_backend)
        if nested is not None:
            return nested
    if (
        source_backend == "ch"
        and target_backend in {"gp", "trino"}
        and classify_source_type(normalize_type_name(column.native_type)) == "timestamp"
    ):
        return ("TIMESTAMP(6)" if target_backend == "trino" else "TIMESTAMP") + " WITH TIME ZONE"
    return str(
        adapter.map_same_backend_source_type_to_target(column)
        if source_backend == target_backend
        else adapter.map_source_type_to_target(column)
    )


def unwrap_type(value: str, wrapper: str) -> str:
    prefix = f"{wrapper}("
    if value.startswith(prefix) and value.endswith(")"):
        return value[len(prefix) : -1].strip()
    return value


def classify_source_type(source_type: str) -> str:
    if not source_type:
        return "string"
    if source_type.split("(", 1)[0].strip() in {"array", "tuple", "map", "row", "nested"}:
        return "string"
    simple_types = {
        "binary": "binary",
        "bytea": "binary",
        "varbinary": "binary",
        "boolean": "boolean",
        "bool": "boolean",
        "date": "date",
        "date32": "date",
    }
    if source_type in simple_types:
        return simple_types[source_type]
    if "timestamp" in source_type or source_type.startswith("datetime"):
        return "timestamp"
    if source_type.startswith(("decimal", "numeric", "number")):
        return "decimal"
    if source_type.startswith(("float", "double", "real")):
        return "float"
    if re.fullmatch(
        r"u?int(?:8|16|32|64|128|256)?|tinyint|smallint|bigint|integer|int[24]", source_type
    ):
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


def type_has_timezone(source_type: str) -> bool:
    return bool(
        "with time zone" in source_type
        or "timestamptz" in source_type
        or re.match(r"datetime64\s*\([^,]+,\s*['\"]", source_type)
        or re.match(r"datetime\s*\(\s*['\"]", source_type)
    )


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
    if base_type.startswith(("Array(", "Tuple(", "Map(", "Nested(")):
        return base_type
    if base_type.startswith("LowCardinality(") and base_type.endswith(")"):
        return "LowCardinality(" + nullable_clickhouse_type(base_type[15:-1]) + ")"
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

    has_null = dict.fromkeys(columns, False)
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


def exact_integer_target(source_type: str, backend: str) -> str | None:
    """Map integer domains structurally, including ClickHouse wide integers."""
    match = re.fullmatch(r"(u?)int(8|16|32|64|128|256)", source_type)
    if match is None:
        return None
    unsigned, bits_text = match.groups()
    bits = int(bits_text)
    # int8 spelling is a PostgreSQL alias only when supplied by that adapter;
    # ClickHouse metadata uses its documented eight-bit domain.
    needed_bits = bits + bool(unsigned)
    for width, target in ((8, "TINYINT"), (16, "SMALLINT"), (32, "INTEGER"), (64, "BIGINT")):
        if needed_bits <= width:
            return "SMALLINT" if backend == "gp" and target == "TINYINT" else target
    precision = len(str(2**bits - 1 if unsigned else 2 ** (bits - 1)))
    if backend == "trino" and precision > _TRINO_MAX_DECIMAL_PRECISION:
        return "VARCHAR"  # Pending complete-value adaptation, never certified DDL.
    return f"{'NUMERIC' if backend == 'gp' else 'DECIMAL'}({precision}, 0)"
