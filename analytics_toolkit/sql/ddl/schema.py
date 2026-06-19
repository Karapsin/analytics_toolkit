from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal

import pandas as pd

from ..backends import UNSUPPORTED_BACKEND_MESSAGE
from ..connection.config import resolve_connection_backend
from ..connection.errors import UnsupportedConnectionTypeError
from .identifiers import quote_identifier


def _build_column_definitions(
    backend: str,
    batch: pd.DataFrame,
    column_types: Mapping[str, str] | None,
) -> str:
    column_defs = []
    for column_name in batch.columns:
        db_type = (
            _explicit_column_type(column_types, column_name)
            if column_types is not None
            else _infer_backend_type(backend, batch[column_name])
        )
        column_defs.append(f"{quote_identifier(column_name, backend)} {db_type}")
    return ", ".join(column_defs)

def _build_expected_ch_column_types(
    batch: pd.DataFrame,
    column_types: Mapping[str, str] | None,
) -> dict[str, str]:
    expected: dict[str, str] = {}
    for column_name in batch.columns:
        column_key = str(column_name)
        expected[column_key] = (
            _explicit_column_type(column_types, column_key)
            if column_types is not None
            else _infer_ch_type(batch[column_name])
        )
    return expected

def build_table_schema_column_definitions(
    connection_type: str,
    table_schema: Mapping[str, str],
    columns: Sequence[str] | None = None,
) -> str:
    backend = resolve_connection_backend(connection_type)
    normalized_schema = normalize_table_schema(table_schema, columns=columns)
    schema_batch = pd.DataFrame(columns=list(normalized_schema))
    return _build_column_definitions(backend, schema_batch, normalized_schema)

def normalize_table_schema(
    table_schema: Mapping[str, str] | None,
    columns: Sequence[str] | None = None,
    *,
    option_name: str = "table_schema",
) -> dict[str, str] | None:
    if table_schema is None:
        return None
    if not isinstance(table_schema, Mapping):
        raise TypeError(
            f"{option_name} must be a mapping of column names to SQL types."
        )

    normalized_schema: dict[str, str] = {}
    for column_name, db_type in table_schema.items():
        if not isinstance(column_name, str) or not column_name.strip():
            raise ValueError(f"{option_name} column names must be non-empty strings.")
        if not isinstance(db_type, str):
            raise TypeError(
                f"SQL type for column {column_name!r} in {option_name} "
                "must be a string."
            )
        normalized_type = db_type.strip()
        if not normalized_type:
            raise ValueError(f"SQL type for column {column_name!r} must not be empty.")
        normalized_schema[column_name] = normalized_type

    if not normalized_schema:
        raise ValueError(f"{option_name} must not be empty when provided.")
    if columns is None:
        return normalized_schema

    return validate_table_schema_columns(
        normalized_schema,
        columns,
        option_name=option_name,
    )

def validate_table_schema_columns(
    table_schema: Mapping[str, str],
    columns: Sequence[str],
    *,
    option_name: str = "table_schema",
) -> dict[str, str]:
    column_names = [str(column) for column in columns]
    column_name_set = set(column_names)
    missing_columns = [
        column_name for column_name in column_names if column_name not in table_schema
    ]
    extra_columns = [
        column_name for column_name in table_schema if column_name not in column_name_set
    ]

    if missing_columns:
        raise ValueError(
            f"{option_name} is missing SQL type for column(s): "
            + ", ".join(missing_columns)
        )
    if extra_columns:
        raise ValueError(
            f"{option_name} contains column(s) not present in data: "
            + ", ".join(extra_columns)
        )
    return {column_name: table_schema[column_name] for column_name in column_names}

def _resolve_create_column_types(
    *,
    table_schema: Mapping[str, str] | None,
    column_types: Mapping[str, str] | None,
    columns: Sequence[str],
) -> Mapping[str, str] | None:
    if table_schema is None:
        return column_types

    normalized_schema = normalize_table_schema(table_schema, columns=columns)
    if column_types is None:
        return normalized_schema

    normalized_column_types = _normalize_column_types_for_columns(
        column_types,
        columns,
    )
    if normalized_schema != normalized_column_types:
        raise ValueError(
            "table_schema and column_types must define the same SQL types "
            "when both are provided."
        )
    return normalized_schema

def _normalize_column_types_for_columns(
    column_types: Mapping[str, str],
    columns: Sequence[str],
) -> dict[str, str]:
    if not isinstance(column_types, Mapping):
        raise TypeError("column_types must be a mapping of column names to SQL types.")
    return {
        str(column_name): _explicit_column_type(column_types, str(column_name))
        for column_name in columns
    }

def _infer_backend_type(backend: str, series: pd.Series) -> str:
    try:
        infer_type = _COLUMN_TYPE_INFERERS[backend]
    except KeyError as exc:
        raise UnsupportedConnectionTypeError(UNSUPPORTED_BACKEND_MESSAGE) from exc
    return infer_type(series)

def _explicit_column_type(
    column_types: Mapping[str, str],
    column_name: str,
) -> str:
    try:
        db_type = column_types[column_name]
    except KeyError as exc:
        raise ValueError(f"Missing explicit SQL type for column {column_name!r}.") from exc
    normalized = db_type.strip()
    if not normalized:
        raise ValueError(f"SQL type for column {column_name!r} must not be empty.")
    return normalized

def _infer_gp_type(series: pd.Series) -> str:
    return _infer_common_sql_type(series)

def _infer_trino_type(series: pd.Series) -> str:
    common_type = _infer_common_sql_type(series)
    if common_type == "DOUBLE PRECISION":
        return "DOUBLE"
    if common_type == "TEXT":
        return "VARCHAR"
    return common_type

def _infer_ch_type(series: pd.Series) -> str:
    if pd.api.types.is_bool_dtype(series):
        base_type = "Bool"
    elif pd.api.types.is_integer_dtype(series):
        base_type = "Int64"
    elif pd.api.types.is_float_dtype(series):
        base_type = "Float64"
    elif pd.api.types.is_datetime64_any_dtype(series):
        base_type = "DateTime64(6)"
    else:
        non_null = series.dropna()
        if not non_null.empty and all(isinstance(value, Decimal) for value in non_null):
            base_type = "Float64"
        elif not non_null.empty and all(
            hasattr(value, "year")
            and hasattr(value, "month")
            and hasattr(value, "day")
            for value in non_null
        ):
            base_type = "Date"
        else:
            base_type = "String"

    if series.isna().any():
        return f"Nullable({base_type})"
    return base_type

def _infer_common_sql_type(series: pd.Series) -> str:
    if pd.api.types.is_bool_dtype(series):
        return "BOOLEAN"
    if pd.api.types.is_integer_dtype(series):
        return "BIGINT"
    if pd.api.types.is_float_dtype(series):
        return "DOUBLE PRECISION"
    if pd.api.types.is_datetime64_any_dtype(series):
        return "TIMESTAMP"

    non_null = series.dropna()
    if not non_null.empty and all(
        hasattr(value, "year") and hasattr(value, "month") and hasattr(value, "day")
        for value in non_null
    ):
        return "DATE"
    return "TEXT"

_COLUMN_TYPE_INFERERS = {
    "gp": _infer_gp_type,
    "trino": _infer_trino_type,
    "ch": _infer_ch_type,
}
