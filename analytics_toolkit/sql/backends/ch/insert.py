from __future__ import annotations

import json
import re
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

import pandas as pd


def insert_dataframe_batch(
    connection: Any,
    table_name: str,
    batch: pd.DataFrame,
    target_column_types: dict[str, str] | None,
    on_progress: Any = None,
) -> None:
    if getattr(connection, "is_native_transport", False):
        columns = list(batch.columns)
        connection.insert(
            table=table_name,
            data=[
                normalize_typed_row(columns, row, target_column_types)
                for row in batch.itertuples(index=False, name=None)
            ],
            column_names=columns,
            column_type_names=column_type_names(columns, target_column_types),
        )
    else:
        normalized_batch = normalize_batch(batch)
        connection.insert_df(
            table=table_name,
            df=normalized_batch,
            column_names=list(batch.columns),
        )
    if on_progress is not None:
        on_progress(len(batch))


def normalize_batch(batch: pd.DataFrame) -> pd.DataFrame:
    map_values = getattr(batch, "map", None)
    if map_values is None:
        normalized = batch.applymap(normalize_scalar)
    else:
        normalized = map_values(normalize_scalar)
    for column_name in normalized.columns:
        series = normalized[column_name]
        normalized[column_name] = series.astype(object).where(series.notna(), None)
    return normalized


def normalize_row(row: Sequence[Any]) -> tuple[Any, ...]:
    return tuple(normalize_scalar(_normalize_nullable_scalar(value)) for value in row)


def normalize_typed_row(
    columns: Sequence[str],
    row: Sequence[Any],
    column_types: dict[str, str] | None,
) -> tuple[Any, ...]:
    normalized = normalize_row(row)
    if not column_types:
        return normalized
    return tuple(
        _normalize_typed_scalar(value, column_types.get(column, ""))
        for column, value in zip(columns, normalized)
    )


def normalize_rows(
    connection: Any,
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
    column_types: dict[str, str] | None,
) -> list[tuple[Any, ...]]:
    if getattr(connection, "is_native_transport", False):
        return [normalize_typed_row(columns, row, column_types) for row in rows]
    return [normalize_row(row) for row in rows]


def _normalize_typed_scalar(value: Any, type_name: str) -> Any:
    if value is None:
        return None
    normalized_type = re.sub(r"^(?:Nullable|LowCardinality)\((.*)\)$", r"\1", type_name)
    if re.fullmatch(r"U?Int(?:8|16|32|64|128|256)", normalized_type):
        return int(value)
    if normalized_type.startswith("Decimal"):
        return Decimal(str(value))
    if normalized_type == "String" and isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return value


def normalize_scalar(value: object) -> object:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, list):
        return [normalize_scalar(item) for item in value]
    if isinstance(value, tuple):
        return tuple(normalize_scalar(item) for item in value)
    if isinstance(value, dict):
        return {normalize_scalar(key): normalize_scalar(item) for key, item in value.items()}
    return value


def column_type_names(
    columns: Sequence[str],
    column_types: dict[str, str] | None,
) -> list[str] | None:
    if column_types is None:
        return None
    try:
        return [column_types[column_name] for column_name in columns]
    except KeyError as exc:
        raise ValueError(
            f"Missing explicit SQL type for column {exc.args[0]!r}."
        ) from exc


def _normalize_nullable_scalar(value: Any) -> Any:
    if _is_null_like(value):
        return None
    return value


def _is_null_like(value: Any) -> bool:
    if value is None:
        return True
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False
