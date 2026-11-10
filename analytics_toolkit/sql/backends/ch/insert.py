from __future__ import annotations

import re
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pandas as pd

if TYPE_CHECKING:
    from collections.abc import Sequence


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
        normalized_batch = normalize_typed_batch(batch, target_column_types)
        connection.insert_df(
            table=table_name,
            df=normalized_batch,
            column_names=list(batch.columns),
        )
    if on_progress is not None:
        on_progress(len(batch))


def normalize_batch(batch: pd.DataFrame) -> pd.DataFrame:
    return _object_dataframe(
        [
            [normalize_scalar(_normalize_nullable_scalar(value)) for value in batch.iloc[:, index]]
            for index in range(len(batch.columns))
        ],
        list(batch.columns),
        batch.index,
    )


def normalize_typed_batch(
    batch: pd.DataFrame,
    column_types: dict[str, str] | None,
) -> pd.DataFrame:
    normalized = normalize_batch(batch)
    if not column_types:
        return normalized
    return _object_dataframe(
        [
            [
                _normalize_typed_scalar(value, column_types.get(column_name, ""))
                for value in normalized.iloc[:, index]
            ]
            for index, column_name in enumerate(normalized.columns)
        ],
        list(normalized.columns),
        normalized.index,
    )


def _object_dataframe(columns: Any, names: Any, index: Any) -> pd.DataFrame:
    frame = pd.DataFrame(
        {
            position: pd.Series(values, index=index, dtype=object)
            for position, values in enumerate(columns)
        },
        index=index,
    )
    frame.columns = names
    return frame


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
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
    column_types: dict[str, str] | None,
) -> list[tuple[Any, ...]]:
    if column_types:
        return [normalize_typed_row(columns, row, column_types) for row in rows]
    return [normalize_row(row) for row in rows]


def _normalize_typed_scalar(value: Any, type_name: str) -> Any:  # noqa: C901, PLR0911 - exact recursive native types
    if _is_null_like(value):
        return None
    from analytics_toolkit.sql.backends.type_resolution import (  # noqa: PLC0415 - backend registry import cycle
        parse_native_type,
    )
    from analytics_toolkit.sql.core.exact_values import (  # noqa: PLC0415 - backend registry import cycle
        OrderedMapPairs,
    )

    parsed = parse_native_type(type_name).base
    normalized_type = parsed.original
    if parsed.name == "array":
        return [_normalize_typed_scalar(item, parsed.arguments[0]) for item in value]
    if parsed.name == "tuple" and not isinstance(value, dict):
        return tuple(
            _normalize_typed_scalar(item, item_type)
            for item, item_type in zip(value, parsed.arguments)
        )
    if parsed.name == "map":
        pairs = [
            (
                _normalize_typed_scalar(key, parsed.arguments[0]),
                _normalize_typed_scalar(item, parsed.arguments[1]),
            )
            for key, item in value.items()
        ]
        return dict(pairs) if isinstance(value, dict) else OrderedMapPairs(pairs)
    if re.fullmatch(r"U?Int(?:8|16|32|64|128|256)", normalized_type):
        return int(value)
    if parsed.name.startswith("decimal"):
        return Decimal(str(value))
    if parsed.name.startswith("float"):
        return float(value)
    if parsed.name == "string":
        from analytics_toolkit.sql.core.exact_values import (  # noqa: PLC0415 - avoid backend registry import cycles
            typed_text,
        )

        if isinstance(value, (memoryview, bytearray)):
            value = bytes(value)
        elif not isinstance(value, (bytes, str)):
            value = typed_text(value)
    return value


def normalize_scalar(value: object) -> object:
    from analytics_toolkit.sql.core.exact_values import (  # noqa: PLC0415 - avoid backend registry import cycles
        OrderedMapPairs,
    )

    if isinstance(value, OrderedMapPairs):
        return OrderedMapPairs(
            (
                normalize_scalar(_normalize_nullable_scalar(key)),
                normalize_scalar(_normalize_nullable_scalar(item)),
            )
            for key, item in value
        )
    if isinstance(value, list):
        return [normalize_scalar(_normalize_nullable_scalar(item)) for item in value]
    if isinstance(value, tuple):
        return tuple(normalize_scalar(_normalize_nullable_scalar(item)) for item in value)
    if isinstance(value, dict):
        return {
            normalize_scalar(_normalize_nullable_scalar(key)): normalize_scalar(
                _normalize_nullable_scalar(item)
            )
            for key, item in value.items()
        }
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
        msg = f"Missing explicit SQL type for column {exc.args[0]!r}."
        raise ValueError(msg) from exc


def _normalize_nullable_scalar(value: Any) -> Any:
    if _is_null_like(value):
        return None
    return value


def _is_null_like(value: Any) -> bool:
    from analytics_toolkit.sql.core.exact_values import (  # noqa: PLC0415 - avoid backend registry import cycles
        is_null,
    )

    return is_null(value)
