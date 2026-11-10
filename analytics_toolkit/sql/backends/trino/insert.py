from __future__ import annotations

from itertools import islice
from typing import TYPE_CHECKING, Any, cast

from sqlparse import lexer, tokens

from analytics_toolkit.sql._log_context import prefix_sql_log_message
from analytics_toolkit.sql.backends.base import _apply_query_label
from analytics_toolkit.sql.backends.execution_outcome import close_cursor

from . import parameters as _parameters

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

    import pandas as pd

DEFAULT_TRINO_INSERT_CHUNK_SIZE = 1000
DATETIME_MICROSECOND_PRECISION = 6


def insert_dataframe_batch(
    adapter: Any,
    connection: Any,
    table_name: str,
    batch: Any,
    *,
    target_column_types: dict[str, str] | None = None,
    trino_insert_chunk_size: int | None = None,
    connection_type: str = "trino",
    query_label: str | None = None,
    on_progress: Callable[[int], None] | None = None,
) -> None:
    rows = list(batch.itertuples(index=False, name=None))
    insert_rows(
        adapter,
        connection,
        table_name,
        batch.columns,
        rows,
        target_column_types=target_column_types,
        trino_insert_chunk_size=trino_insert_chunk_size,
        connection_type=connection_type,
        query_label=query_label,
        on_progress=on_progress,
    )


def insert_rows(
    adapter: Any,
    connection: Any,
    table_name: str,
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
    *,
    target_column_types: dict[str, str] | None = None,
    trino_insert_chunk_size: int | None = None,
    connection_type: str = "trino",
    query_label: str | None = None,
    on_progress: Callable[[int], None] | None = None,
) -> None:
    from analytics_toolkit.general import time_print

    chunk_size = get_insert_chunk_size(trino_insert_chunk_size, connection_type)
    cursor = connection.cursor()
    try:
        row_iterator = iter_row_values(columns, rows, target_column_types)
        for row_chunk in _chunk_rows(row_iterator, chunk_size):
            params = [value for row in row_chunk for value in row]
            sql = build_typed_insert_sql(
                adapter,
                table_name,
                columns,
                target_column_types,
                row_count=len(row_chunk),
                query_label=query_label,
            )
            time_print(
                prefix_sql_log_message(f"Writing {len(row_chunk)} row(s) to table {table_name}"),
                backend=adapter.backend,
            )
            cursor.execute(sql, params)
            if on_progress is not None:
                on_progress(len(row_chunk))
    finally:
        close_cursor(cursor, backend="trino")


def get_insert_chunk_size(
    explicit_value: int | None,
    connection_type: str = "trino",
) -> int:
    if explicit_value is not None:
        if explicit_value <= 0:
            raise ValueError("trino_insert_chunk_size must be a positive integer.")
        return explicit_value

    from ...connection.config import TrinoConfig, get_connection_config
    from ...connection.errors import SqlConfigError, UnsupportedConnectionTypeError

    try:
        config = get_connection_config(connection_type)
    except (SqlConfigError, UnsupportedConnectionTypeError):
        return DEFAULT_TRINO_INSERT_CHUNK_SIZE
    if isinstance(config, TrinoConfig) and config.insert_chunk_size is not None:
        return config.insert_chunk_size
    return DEFAULT_TRINO_INSERT_CHUNK_SIZE


def cast_parameter_placeholders(
    sql: str,
    columns: Sequence[str],
    target_column_types: dict[str, str] | None,
    *,
    row_count: int,
) -> str:
    if not target_column_types:
        return sql
    casts = [
        f"CAST(? AS {target_column_types[column]})" if column in target_column_types else "?"
        for _ in range(row_count)
        for column in columns
    ]
    fragments: list[str] = []
    tokenize = cast("Callable[[str], Iterator[tuple[Any, str]]]", lexer.tokenize)
    for token_type, value in tokenize(sql):
        if token_type in tokens.Name.Placeholder and value == "?":
            if not casts:
                message = "Too many executable parameter placeholders."
                raise ValueError(message)
            fragments.append(casts.pop(0))
        else:
            fragments.append(value)
    if casts:
        message = "Missing executable parameter placeholders."
        raise ValueError(message)
    return "".join(fragments)


def iter_dataframe_rows(
    batch: pd.DataFrame,
    target_column_types: dict[str, str] | None,
) -> Iterator[tuple[Any, ...]]:
    for row in batch.itertuples(index=False, name=None):
        yield from iter_row_values(batch.columns, [row], target_column_types)


def iter_row_values(
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
    target_column_types: dict[str, str] | None,
) -> Iterator[tuple[Any, ...]]:
    for row in rows:
        validate_row_width(columns, row)
        normalized_values = []
        for column_name, value in zip(columns, row):
            target_type = (
                target_column_types.get(column_name) if target_column_types is not None else None
            )
            normalized_values.append(normalize_value(value, target_type))
        yield tuple(normalized_values)


normalize_value = _parameters.normalize_value


def build_values_tuple(
    columns: Sequence[str],
    row: Sequence[Any],
    target_column_types: dict[str, str] | None,
) -> str:
    validate_row_width(columns, row)
    values_sql = []
    for column_name, value in zip(columns, row):
        target_type = (
            target_column_types.get(column_name) if target_column_types is not None else None
        )
        values_sql.append(literal(value, target_type))
    return f"({', '.join(values_sql)})"


def validate_row_width(columns: Sequence[str], row: Sequence[Any]) -> None:
    if len(columns) != len(row):
        raise ValueError("Column and row value counts must match.")


literal = _parameters.literal


def _chunk_rows(
    rows: Iterator[tuple[Any, ...]],
    chunk_size: int,
) -> Iterator[list[tuple[Any, ...]]]:
    while True:
        chunk = list(islice(rows, chunk_size))
        if not chunk:
            return
        yield chunk


_is_null_like = _parameters.is_null


def build_typed_insert_sql(  # noqa: PLR0913 -- mirrors insert batch contract.
    adapter: Any,
    table_name: str,
    columns: Sequence[str],
    target_column_types: dict[str, str] | None,
    *,
    row_count: int,
    query_label: str | None = None,
) -> str:
    if row_count <= 0:
        message = "row_count must be a positive integer."
        raise ValueError(message)
    types = target_column_types or {}
    parameters = [f"CAST(? AS {types[name]})" if name in types else "?" for name in columns]
    row = "(" + ", ".join(parameters) + ")"
    values = ", ".join(row for _ in range(row_count))
    return _apply_query_label(
        f"INSERT INTO {table_name} ({adapter.column_list_sql(columns)}) VALUES {values}",  # noqa: S608 -- identifier SQL; values remain bound.
        query_label,
    )
