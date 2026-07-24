from __future__ import annotations

from collections.abc import Mapping, Sequence

import pandas as pd

from ..backends import get_backend_adapter
from ..connection.config import resolve_connection_backend
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
    return get_backend_adapter(backend).infer_dataframe_column_type(series)

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
