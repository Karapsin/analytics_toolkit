from __future__ import annotations

import datetime as dt
import re
from typing import TYPE_CHECKING, Any

from .. import source_schema as _source_schema

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ..models import SourceColumn

_CLICKHOUSE_MAX_DECIMAL_PRECISION = 76
_CLICKHOUSE_DATETIME_TIMEZONE = re.compile(
    r"datetime64\s*\([^,]+,\s*['\"]([^'\"]+)['\"]",
    re.IGNORECASE,
)


def inspect_source_query_schema(adapter: Any, connection: Any, query: str) -> list[Any]:
    del adapter
    return _source_schema.inspect_clickhouse_source_schema(connection, query)


def map_source_type_to_target(adapter: Any, column: SourceColumn) -> str:
    del adapter
    from analytics_toolkit.sql.core.type_resolution import (  # noqa: PLC0415 - avoid backend registry import cycles
        parse_native_type,
    )

    parsed = parse_native_type(column.native_type).base
    integer = re.fullmatch(r"(u?)int(8|16|32|64|128|256)", parsed.name)
    if integer:
        return _source_schema.nullable_clickhouse_type(
            ("UInt" if integer.group(1) else "Int") + integer.group(2)
        )
    if parsed.name in {
        "date32",
        "datetime",
        "datetime64",
        "array",
        "tuple",
        "map",
        "nested",
        "uuid",
    }:
        canonical = {
            "date32": "Date32",
            "datetime": "DateTime",
            "datetime64": "DateTime64",
            "uuid": "UUID",
        }.get(parsed.name)
        native_type = (
            parsed.original
            if canonical is None
            else canonical + ("(" + ", ".join(parsed.arguments) + ")" if parsed.arguments else "")
        )
        return _source_schema.nullable_clickhouse_type(native_type)
    source_type = _source_schema.normalize_type_name(column.native_type)
    precision, scale = _source_schema.type_precision_scale(column, source_type)
    kind = _source_schema.classify_source_type(source_type)
    base_type = _map_to_ch_base_type(
        kind,
        source_type,
        precision,
        scale,
    )
    return _source_schema.nullable_clickhouse_type(base_type)


def map_same_backend_source_type_to_target(adapter: Any, column: SourceColumn) -> str:
    if column.native_type and column.native_type.strip().lower() != "unknown":
        return column.native_type.strip()
    return map_source_type_to_target(adapter, column)


def refine_stage_column_types_from_rows(
    adapter: Any,
    column_types: dict[str, str] | None,
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
) -> dict[str, str] | None:
    del adapter
    return _source_schema.refine_clickhouse_column_types_nullability_from_rows(
        column_types,
        columns,
        rows,
    )


def normalize_transfer_source_batch(
    adapter: Any,
    batch: Any,
    source_column_types: dict[str, str | None],
) -> Any:
    """Attach native ClickHouse timezone metadata to naive DateTime64 values."""
    del adapter
    timezone_by_index: dict[int, dt.tzinfo] = {}
    for index, column in enumerate(batch.columns):
        native_type = source_column_types.get(column) or ""
        match = _CLICKHOUSE_DATETIME_TIMEZONE.search(native_type)
        if match is not None and match.group(1).upper() == "UTC":
            timezone_by_index[index] = dt.timezone.utc
    if not timezone_by_index:
        return batch

    rows: list[tuple[Any, ...]] = []
    for row in batch.rows:
        values = list(row)
        for index, timezone in timezone_by_index.items():
            value = values[index]
            if isinstance(value, dt.datetime) and value.tzinfo is None:
                values[index] = value.replace(tzinfo=timezone)
        rows.append(tuple(values))
    return type(batch)(columns=batch.columns, rows=rows)


def mark_upsert_finalization_error(adapter: Any, exc: Exception) -> None:
    del adapter
    exc.__dict__["analytics_toolkit_sql_retry_safe"] = False


def _map_to_ch_base_type(
    kind: str,
    source_type: str,
    precision: int | None,
    scale: int | None,
) -> str:
    if kind == "binary":
        return "String"
    if kind == "boolean":
        return "Bool"
    if kind == "integer":
        exact = re.fullmatch(r"(u?)int(8|16|32|64|128|256)", source_type)
        if exact:
            return ("UInt" if exact.group(1) else "Int") + exact.group(2)
        return {
            "smallint": "Int16",
            "int2": "Int16",
            "integer": "Int32",
            "int": "Int32",
            "int4": "Int32",
        }.get(source_type, "Int64")
    if kind == "float":
        if source_type in {"real", "float4", "float32"}:
            return "Float32"
        return "Float64"
    if kind == "decimal":
        return _source_schema.decimal_type(
            "Decimal",
            precision,
            scale,
            fallback="Decimal(38, 10)",
            max_precision=_CLICKHOUSE_MAX_DECIMAL_PRECISION,
        )
    if kind == "date":
        return "Date"
    if kind == "timestamp":
        return "DateTime64(6)"
    if kind == "uuid":
        return "UUID"
    return "String"
