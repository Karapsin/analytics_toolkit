from __future__ import annotations

from collections.abc import Sequence
import math
from typing import cast

import pandas as pd
import sqlparse

from ..backends import get_backend_adapter, get_backend_capability
from ..connection.config import get_connection_config
from ..connection.errors import InvalidSqlInputError
from ..dml.io.read_sql import read_sql
from ..execution.operation_runner import timed_public_sql_function


_SHOW_TABLES_COLUMNS = ["db", "schema", "table_name", "row_count", "table_size"]
_ROW_COUNT_COLUMN = "row_count"
_TABLE_SIZE_BYTES_COLUMN = "table_size_bytes"
_TABLE_SIZE_UNITS = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")


@timed_public_sql_function
def show_tables(
    db_key: str,
    schema: str | None = None,
    conditions: str | None = None,
    table_name: str | Sequence[str] | None = None,
    ch_distributed_table_stats: bool = False,
    trino_catalog: str | None = None,
) -> pd.DataFrame:
    """Return backend table metadata with row_count and table_size columns."""

    _validate_bool(
        ch_distributed_table_stats,
        "ch_distributed_table_stats",
    )
    config = get_connection_config(db_key)
    capability = get_backend_capability(config.backend)
    schema_filter = _validate_optional_string(schema, "schema")
    table_name_filter = _validate_table_names(table_name, schema_filter)
    conditions_filter = _validate_conditions(conditions)
    trino_catalog_filter = _validate_optional_string(
        trino_catalog,
        "trino_catalog",
    )
    if (
        trino_catalog_filter is not None
        and not capability.supports_show_tables_catalog_filter
    ):
        raise InvalidSqlInputError(
            "trino_catalog is only supported for Trino connections."
        )

    adapter = get_backend_adapter(config.backend)
    query = adapter.build_show_tables_query(
        config,
        schema_filter,
        table_name_filter,
        conditions_filter,
        ch_distributed_table_stats=ch_distributed_table_stats,
        trino_catalog=trino_catalog_filter,
    )

    result = cast(pd.DataFrame, read_sql(config.connection_key, query))
    if result.empty:
        return pd.DataFrame(columns=_SHOW_TABLES_COLUMNS)

    normalized = adapter.postprocess_show_tables(
        config.connection_key,
        result.copy(),
        ch_distributed_table_stats=ch_distributed_table_stats,
        read_sql=read_sql,
    )
    normalized["row_count"] = pd.Series(
        (_normalize_row_count(value) for value in normalized[_ROW_COUNT_COLUMN]),
        index=normalized.index,
        dtype=object,
    )
    normalized["table_size"] = normalized[_TABLE_SIZE_BYTES_COLUMN].map(
        _format_table_size,
    )
    return normalized.loc[:, _SHOW_TABLES_COLUMNS].copy()


def _validate_optional_string(value: str | None, parameter_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{parameter_name} must be a string or None.")

    normalized = value.strip()
    if not normalized:
        raise InvalidSqlInputError(f"{parameter_name} must not be empty.")
    return normalized


def _validate_bool(value: bool, parameter_name: str) -> None:
    if not isinstance(value, bool):
        raise TypeError(f"{parameter_name} must be a boolean.")


def _validate_table_names(
    table_name: str | Sequence[str] | None,
    schema: str | None,
) -> list[str] | None:
    if table_name is None:
        return None
    if isinstance(table_name, str):
        return [_validate_table_name_value(table_name, schema)]
    if isinstance(table_name, (bytes, bytearray)) or not isinstance(
        table_name,
        Sequence,
    ):
        raise TypeError("table_name must be a string, a sequence of strings, or None.")
    if not table_name:
        raise InvalidSqlInputError("table_name must not be empty.")

    return [
        _validate_table_name_value(value, schema)
        for value in table_name
    ]


def _validate_table_name_value(value: str, schema: str | None) -> str:
    if not isinstance(value, str):
        raise TypeError("table_name values must be strings.")
    normalized = value.strip()
    if not normalized:
        raise InvalidSqlInputError("table_name values must not be empty.")
    if schema is not None and normalized.startswith(f"{schema}."):
        normalized = normalized[len(schema) + 1 :]
        if not normalized:
            raise InvalidSqlInputError("table_name values must not be empty.")
    return normalized


def _validate_conditions(conditions: str | None) -> str | None:
    normalized = _validate_optional_string(conditions, "conditions")
    if normalized is None:
        return None

    statements = [
        statement.strip()
        for statement in sqlparse.split(normalized)
        if statement.strip()
    ]
    if len(statements) != 1:
        raise InvalidSqlInputError(
            "conditions must contain exactly one SQL expression."
        )

    expression = statements[0].rstrip(";").rstrip()
    if not expression:
        raise InvalidSqlInputError("conditions must not be empty.")
    return expression


def _format_table_size(value: object) -> str | None:
    if pd.isna(value):
        return None

    try:
        size = float(value)
    except (TypeError, ValueError):
        return str(value)

    if not math.isfinite(size):
        return None

    sign = "-" if size < 0 else ""
    size = abs(size)
    unit_index = 0
    while size >= 1024 and unit_index < len(_TABLE_SIZE_UNITS) - 1:
        size /= 1024
        unit_index += 1

    unit = _TABLE_SIZE_UNITS[unit_index]
    if unit == "B":
        return f"{sign}{int(round(size))} {unit}"
    return f"{sign}{size:.2f} {unit}"


def _normalize_row_count(value: object) -> int | None:
    if pd.isna(value):
        return None

    try:
        count = float(value)
    except (TypeError, ValueError):
        return None

    if not math.isfinite(count) or count < 0:
        return None
    return int(round(count))


__all__ = ["show_tables"]
