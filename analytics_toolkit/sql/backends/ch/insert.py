from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Any

import pandas as pd


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
